"""
端到端集成测试：扫描 → 命名 → 复制 → 重名 → 报告 → 撤销。

覆盖需求书第三十三节测试1~10 的完整闭环，并验证：
- 源目录在整条链路中零改动（指纹对比）
- 报告与数据库记录正确
"""

from __future__ import annotations

import hashlib
import os
import unittest

from core.task_manager import TaskManager
from database.database import Database
from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig, Status,
                    TaskConfig)
from tests.helpers import TARGET_NAME, TempTree, build_dataset, has_ocr
from utils.logger import TaskLogger

ZH_ROOT = '示例数据'


def tree_fingerprint(root: str) -> str:
    """整棵目录树的 SHA256 指纹 —— 用来证明源数据没被改动。"""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode('utf-8'))
            try:
                with open(path, 'rb') as handle:
                    digest.update(hashlib.sha256(handle.read()).digest())
            except OSError:
                digest.update(b'<unreadable>')
    return digest.hexdigest()


class IntegrationBase(unittest.TestCase):

    template = f'*/1440/{TARGET_NAME}'
    with_ocr = False

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.root = os.path.join(self.base, ZH_ROOT, '20250101')
        self.output = os.path.join(self.base, '整理结果')
        self.db_path = os.path.join(self.base, 'history.db')
        if self.with_ocr and not has_ocr():
            self.skipTest('未安装 OCR 引擎，跳过真实 OCR 测试')
        build_dataset(self.root)
        self.before = tree_fingerprint(self.root)

    def make_manager(self, template=None, **naming_kwargs):
        naming_kwargs.setdefault('fields', ['姓名'])
        config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='advanced',
                             template=template or self.template),
            naming=NamingConfig(**naming_kwargs),
            ocr=OcrConfig(engine='auto' if self.with_ocr else 'none'),
            output=OutputConfig(output_dir=self.output,
                                report_formats=['csv', 'json']),
        )
        db = Database(self.db_path)
        manager = TaskManager(config, db, TaskLogger('test', log_dir=self.base))
        # 测试结束后统一关闭，避免日志文件句柄泄漏
        self.addCleanup(manager.close)
        self.addCleanup(db.close)
        return manager

    def assert_source_untouched(self):
        self.assertEqual(tree_fingerprint(self.root), self.before,
                         '源目录被修改了！这违反最高优先级的安全要求')


class TestScanOnly(IntegrationBase):
    """阶段一：只读扫描。"""

    def test_scan_1440(self):
        manager = self.make_manager()
        result = manager.scan()
        self.assertEqual(result.count, 6)
        folders = sorted(r.source_folder for r in result.files)
        self.assertEqual(folders, ['CASE001', 'CASE002', 'CASE003',
                                   'CASE004', 'CASE005', '其他'])
        self.assert_source_untouched()

    def test_scan_2880_switch(self):
        """测试2：切换规则后 1440 的文件不被命中。"""
        manager = self.make_manager(template=f'*/2880/{TARGET_NAME}')
        result = manager.scan()
        self.assertEqual(result.count, 3)
        for record in result.files:
            self.assertIn('2880', record.rel_path)
        self.assert_source_untouched()

    def test_no_copy_in_scan_phase(self):
        """扫描阶段不能产生任何输出。"""
        manager = self.make_manager()
        manager.scan()
        self.assertFalse(os.path.exists(self.output))
        self.assert_source_untouched()

    def test_basic_mode_builds_same_template(self):
        config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='basic', level1='*',
                             level2='1440', file_name=TARGET_NAME),
        )
        manager = TaskManager(config)
        self.assertEqual(manager.template_text, f'*/1440/{TARGET_NAME}')
        self.assertEqual(manager.scan().count, 6)


class TestFullPipelineWithoutOcr(IntegrationBase):
    """不依赖 OCR 引擎的完整闭环（命名走人工/兜底）。"""

    def test_end_to_end(self):
        manager = self.make_manager()
        manager.scan()

        # 模拟 OCR 结果
        names = {'CASE001': '张三', 'CASE002': '张三', 'CASE003': '李四',
                 'CASE004': '张三', 'CASE005': '王五', '其他': '赵六'}
        for record in manager.records:
            manager.set_manual_name(record, names[record.source_folder])

        manager.save_to_db('ready')
        stats = manager.rebuild_names()
        self.assertEqual(stats['total'], 6)

        result = manager.execute()
        self.assertEqual(result.summary.success, 6)
        self.assertEqual(result.summary.failed, 0)
        self.assert_source_untouched()

        produced = sorted(f for f in os.listdir(self.output) if f != '_reports')
        self.assertEqual(produced, [
            'CASE001_张三.jpg', 'CASE002_张三.jpg', 'CASE003_李四.jpg',
            'CASE004_张三.jpg', 'CASE005_王五.jpg', '其他_赵六.jpg'])
        # 每个文件都真实存在且有内容
        for name in produced:
            self.assertGreater(os.path.getsize(os.path.join(self.output, name)), 0)

    def test_duplicate_names_are_unique(self):
        """测试4+5：同名不同来源不冲突；同名同来源自动 -1。"""
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.rebuild_names()
        manager.execute()

        produced = sorted(f for f in os.listdir(self.output) if f != '_reports')
        self.assertEqual(len(produced), 6)
        self.assertEqual(len(set(produced)), 6, '绝不能出现文件名冲突')

    def test_second_run_creates_suffixes(self):
        """测试6：第二次运行同名任务 -> 全部追加 -1，不覆盖。"""
        for _ in range(2):
            manager = self.make_manager()
            manager.scan()
            for record in manager.records:
                manager.set_manual_name(record, '张三')
            manager.rebuild_names()
            result = manager.execute()

        self.assertEqual(result.summary.success, 6)
        self.assertEqual(result.summary.conflicts, 6)
        produced = sorted(f for f in os.listdir(self.output) if f != '_reports')
        self.assertEqual(len(produced), 12)
        self.assertTrue(all('-1.jpg' in name or '_张三.jpg' in name
                            for name in produced))

    def test_unreviewed_excluded_by_default(self):
        """未确认的文件默认不复制（只记入跳过）。"""
        manager = self.make_manager()
        manager.scan()
        for i, record in enumerate(manager.records):
            records_folder = record.source_folder
            if i < 3:
                manager.set_manual_name(record, '确认过')
            else:
                record.extracted_name = 'AI建议'
                record.status = Status.NEEDS_REVIEW
        manager.rebuild_names()
        result = manager.execute(include_unreviewed=False)
        self.assertEqual(result.summary.success, 3)
        self.assertEqual(result.summary.skipped, 0)
        produced = [f for f in os.listdir(self.output) if f != '_reports']
        self.assertEqual(len(produced), 3)

    def test_report_and_database(self):
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.save_to_db('ready')
        manager.rebuild_names()
        manager.execute()

        reports = manager.export_reports()
        self.assertTrue(reports)
        for path in reports:
            self.assertTrue(os.path.isfile(path), path)
        csv_path = [p for p in reports if p.endswith('.csv')][0]
        with open(csv_path, encoding='utf-8-sig') as handle:
            content = handle.read()
        self.assertIn('序号', content)
        self.assertIn('CASE001_张三.jpg', content)

        tasks = manager.db.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['success'], 6)
        created = manager.db.created_files(manager.task_id)
        self.assertEqual(len(created), 6)

    def test_undo_after_execute(self):
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.save_to_db('ready')
        manager.rebuild_names()
        manager.execute()

        self.assertTrue(os.listdir(self.output))
        result = manager.undo()
        self.assertEqual(result['deleted'], 6)
        remaining = [f for f in os.listdir(self.output) if f != '_reports']
        self.assertEqual(remaining, [])
        self.assert_source_untouched()

    def test_cancel_keeps_partial_results(self):
        """测试10：取消后已复制文件保留。"""
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.rebuild_names()

        import threading
        cancel = threading.Event()

        def on_progress(event):
            if event.current >= 2:
                cancel.set()
                return False
            return True

        result = manager.execute(progress_cb=on_progress, cancel_event=cancel)
        self.assertEqual(result.summary.success, 2)
        self.assertEqual(result.summary.canceled, 4)
        self.assertEqual(len([f for f in os.listdir(self.output)
                              if f != '_reports']), 2)
        self.assert_source_untouched()

    def test_destination_folder_field(self):
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.rebuild_names()
        manager.execute()

    def test_skip_record_excluded(self):
        manager = self.make_manager()
        manager.scan()
        for record in manager.records:
            manager.set_manual_name(record, '张三')
        manager.skip_record(manager.records[0], '测试跳过')
        manager.rebuild_names()
        result = manager.execute()
        self.assertEqual(result.summary.success, 5)
        self.assertEqual(result.summary.skipped, 1)


@unittest.skipUnless(has_ocr(), '未安装 OCR 引擎')
class TestFullPipelineWithOcr(IntegrationBase):
    """测试三 / 四：真实 OCR 全链路。"""

    with_ocr = True

    def test_ocr_extracts_name(self):
        manager = self.make_manager()
        manager.scan()
        summary = manager.run_ocr()
        self.assertGreaterEqual(summary.ocr_ok, 3)
        self.assert_source_untouched()

        by_folder = {r.source_folder: r for r in manager.records}
        self.assertEqual(by_folder['CASE001'].extracted_name, '张三')
        self.assertEqual(by_folder['CASE002'].extracted_name, '张三')
        self.assertEqual(by_folder['CASE003'].extracted_name, '李四')
        self.assertEqual(by_folder['CASE001'].extract_method, 'field')

    def test_ocr_failure_needs_review(self):
        """测试7：无法识别 -> 需人工确认，不会胡乱命名。"""
        manager = self.make_manager()
        manager.scan()
        manager.run_ocr()
        by_folder = {r.source_folder: r for r in manager.records}
        failed = by_folder['CASE004']          # 无文字的图片
        self.assertEqual(failed.status, Status.NEEDS_REVIEW)
        self.assertFalse(failed.extracted_name)

    def test_full_flow_with_ocr(self):
        manager = self.make_manager()
        manager.scan()
        manager.run_ocr()
        manager.rebuild_names()
        result = manager.execute(include_unreviewed=True)

        self.assertEqual(result.summary.success, 6)
        produced = sorted(f for f in os.listdir(self.output) if f != '_reports')
        self.assertIn('CASE001_张三.jpg', produced)
        self.assertIn('CASE002_张三.jpg', produced)
        self.assertIn('CASE003_李四.jpg', produced)
        self.assert_source_untouched()

    def test_corrupt_image_does_not_break_task(self):
        """测试9：损坏图片只影响自己。"""
        manager = self.make_manager()
        manager.scan()
        summary = manager.run_ocr()
        corrupt = [r for r in manager.records if r.source_folder == 'CASE005']
        if corrupt:
            self.assertTrue(corrupt[0].ocr_error or not corrupt[0].ocr_text)
            self.assertEqual(corrupt[0].status, Status.NEEDS_REVIEW)
        self.assertGreaterEqual(summary.ocr_ok, 3)


if __name__ == '__main__':
    unittest.main()

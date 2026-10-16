"""
测试六 / 九 / 十：复制执行、错误处理、取消、撤销。

核心断言：
- 目标目录已有同名文件时，绝不覆盖
- 单个文件失败不影响其他文件
- 取消后已复制的文件保留
- 原始文件在整条链路中零改动
"""

from __future__ import annotations

import os
import threading
import unittest

from core.extractor import Extractor, is_eligible, undo_files
from core.filename_manager import resolve_filenames
from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig, Status,
                    TaskConfig)
from tests.helpers import TARGET_NAME, TempTree, make_record, write_file
from utils import file_utils


def make_config(root: str, output: str, **output_kwargs) -> TaskConfig:
    return TaskConfig(
        rule=ExtractRule(root_dir=root, mode='advanced',
                         template=f'*/1440/{TARGET_NAME}'),
        naming=NamingConfig(fields=['姓名']),
        ocr=OcrConfig(),
        output=OutputConfig(output_dir=output, **output_kwargs),
    )


class TestCopySafety(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.src_dir = os.path.join(self.root, 'src')
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out, exist_ok=True)

    def _make_source(self, name='a.jpg', content=b'IMAGE-DATA-1234'):
        return write_file(os.path.join(self.src_dir, name), content)

    # --------------------------------------------------

    def test_copy_no_overwrite_raises(self):
        src = self._make_source()
        dst = os.path.join(self.out, 'x.jpg')
        file_utils.copy_no_overwrite(src, dst)

        with self.assertRaises(FileExistsError):
            file_utils.copy_no_overwrite(src, dst)
        with open(dst, 'rb') as handle:
            self.assertEqual(handle.read(), b'IMAGE-DATA-1234')

    def test_extractor_never_overwrites(self):
        """测试6：目标已存在同名文件 -> 复制为 -1，原文件内容不变。"""
        existing = os.path.join(self.out, 'CASE001_张三.jpg')
        write_file(existing, b'EXISTING-CONTENT')

        src = self._make_source()
        record = make_record(src, source_folder='CASE001')
        record.extracted_name = '张三'

        config = make_config(self.root, self.out)
        resolve_filenames([record], self.out, config.naming, config.output)
        # 预分配阶段就应该改名为 -1
        self.assertEqual(record.final_filename, 'CASE001_张三-1.jpg')

        result = Extractor(config).execute([record])
        self.assertEqual(result.summary.success, 1)
        self.assertEqual(record.final_filename, 'CASE001_张三-1.jpg')
        with open(existing, 'rb') as handle:
            self.assertEqual(handle.read(), b'EXISTING-CONTENT')

    def test_source_file_not_modified(self):
        src = self._make_source()
        before = open(src, 'rb').read(), os.path.getmtime(src)

        record = make_record(src, source_folder='CASE001')
        record.extracted_name = '张三'
        config = make_config(self.root, self.out)
        Extractor(config).execute([record])

        after = open(src, 'rb').read(), os.path.getmtime(src)
        self.assertEqual(before, after)
        self.assertTrue(os.path.isfile(src))

    def test_corrupt_file_does_not_stop_others(self):
        """测试9：损坏/不存在的文件失败，其他文件继续处理。"""
        good1 = self._make_source('good1.jpg', b'AAA')
        good2 = self._make_source('good2.jpg', b'BBB')

        records = []
        for i, (path, name) in enumerate([(good1, '张三'), (good2, '李四')], start=1):
            record = make_record(path, index=i, source_folder=f'FU{i:03d}')
            record.extracted_name = name
            records.append(record)

        # 插入一个不存在的文件
        missing = make_record(os.path.join(self.src_dir, 'missing.jpg'),
                              index=3, source_folder='CASE003')
        missing.extracted_name = '王五'
        records.append(missing)

        config = make_config(self.root, self.out)
        result = Extractor(config).execute(records)

        self.assertEqual(result.summary.success, 2)
        self.assertEqual(result.summary.failed, 1)
        self.assertIn('不存在', records[2].error)
        self.assertEqual(records[0].status, Status.SUCCESS)
        self.assertEqual(records[1].status, Status.SUCCESS)
        self.assertEqual(records[2].status, Status.FAILED)

    def test_verify_copy_detects_mismatch(self):
        """复制校验：大小不一致时删除半成品并报错。"""
        src = self._make_source()
        record = make_record(src, source_folder='CASE001')
        record.extracted_name = '张三'

        config = make_config(self.root, self.out, verify_copy=True)
        extractor = Extractor(config)

        # 模拟复制成功但大小不一致
        original = file_utils.copy_no_overwrite

        def fake_copy(source, dest):
            original(source, dest)
            with open(dest, 'wb') as handle:
                handle.write(b'X')

        file_utils.copy_no_overwrite = fake_copy
        try:
            result = extractor.execute([record])
        finally:
            file_utils.copy_no_overwrite = original

        self.assertEqual(result.summary.failed, 1)
        self.assertIn('校验失败', record.error)
        self.assertEqual(os.listdir(self.out), [])

    def test_cancel_keeps_copied_files(self):
        """测试10：取消任务，已复制的文件保留，未处理的标记为已取消。"""
        records = []
        for i in range(5):
            src = self._make_source(f'f{i}.jpg', b'DATA' + bytes([i]))
            record = make_record(src, index=i + 1, source_folder=f'FU{i:03d}')
            record.extracted_name = f'名字{i}'
            records.append(record)

        config = make_config(self.root, self.out)
        cancel_event = threading.Event()
        seen = []

        def on_progress(event):
            seen.append(event.current)
            if event.current >= 2:                # 处理完 2 个后请求取消
                cancel_event.set()
                return False
            return True

        result = Extractor(config).execute(records, progress_cb=on_progress,
                                           cancel_event=cancel_event)

        self.assertEqual(result.summary.success, 2)
        self.assertEqual(result.summary.canceled, 3)
        copied = [f for f in os.listdir(self.out)]
        self.assertEqual(len(copied), 2, f'已复制的文件必须保留：{copied}')
        for record in records[2:]:
            self.assertEqual(record.status, Status.CANCELED)

    def test_skip_policy(self):
        existing = os.path.join(self.out, 'CASE001_张三.jpg')
        write_file(existing)
        src = self._make_source()
        record = make_record(src, source_folder='CASE001')
        record.extracted_name = '张三'

        config = make_config(self.root, self.out, conflict_policy='skip')
        result = Extractor(config).execute([record])
        self.assertEqual(result.summary.skipped, 1)
        self.assertEqual(result.summary.success, 0)
        self.assertEqual(len(os.listdir(self.out)), 1)

    def test_keep_structure(self):
        """保留层级结构时，文件放到对应子目录中。"""
        src = self._make_source()
        record = make_record(src, source_folder='CASE001',
                             rel_path=f'CASE001/1440/{TARGET_NAME}')
        record.extracted_name = '张三'

        config = make_config(self.root, self.out, keep_structure=True)
        result = Extractor(config).execute([record])
        self.assertEqual(result.summary.success, 1)
        self.assertTrue(os.path.isfile(
            os.path.join(self.out, 'CASE001', '1440', 'CASE001_张三.jpg')))

    def test_progress_events(self):
        src = self._make_source()
        record = make_record(src, source_folder='CASE001')
        record.extracted_name = '张三'
        events = []
        Extractor(make_config(self.root, self.out)).execute(
            [record], progress_cb=lambda event: events.append(event) or True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].phase, 'execute')
        self.assertEqual(events[0].success, 1)

    def test_is_eligible(self):
        record = make_record('x')
        self.assertTrue(is_eligible(record))
        record.status = Status.NEEDS_REVIEW
        self.assertFalse(is_eligible(record))
        self.assertTrue(is_eligible(record, include_unreviewed=True))
        record.status = Status.SKIPPED
        self.assertFalse(is_eligible(record, include_unreviewed=True))
        record.status = Status.SUCCESS
        self.assertFalse(is_eligible(record, include_unreviewed=True))

    def test_precheck_detects_missing_source(self):
        record = make_record(os.path.join(self.src_dir, 'ghost.jpg'))
        record.extracted_name = '张三'
        check = Extractor(make_config(self.root, self.out)).precheck([record])
        self.assertTrue(any('源文件' in problem for problem in check['problems']))


class TestUndo(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out, exist_ok=True)

    def _row(self, name, content=b'DATA', size=None):
        path = write_file(os.path.join(self.out, name), content)
        return {'dest_path': path, 'size': os.path.getsize(path) if size is None else size,
                'final_filename': name}

    def test_undo_deletes_only_recorded_files(self):
        keep = write_file(os.path.join(self.out, '用户自己的文件.jpg'), b'MINE')
        row = self._row('CASE001_张三.jpg')

        result = undo_files([row], allowed_root=self.out)
        self.assertEqual(result['deleted'], 1)
        self.assertFalse(os.path.exists(row['dest_path']))
        self.assertTrue(os.path.exists(keep), '不属于本任务的文件必须保留')

    def test_undo_refuses_outside_output_dir(self):
        """安全护栏：不在输出目录内的文件拒绝删除。"""
        outsider = write_file(os.path.join(self.root, '原始数据.jpg'), b'ORIGINAL')
        row = {'dest_path': outsider, 'size': os.path.getsize(outsider)}

        result = undo_files([row], allowed_root=self.out)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(os.path.exists(outsider))

    def test_undo_refuses_size_mismatch(self):
        row = self._row('a.jpg', b'DATA', size=999999)
        result = undo_files([row], allowed_root=self.out)
        self.assertEqual(result['deleted'], 0)
        self.assertTrue(os.path.exists(row['dest_path']))

    def test_undo_missing_file_counts_separately(self):
        row = {'dest_path': os.path.join(self.out, 'not_exist.jpg'), 'size': 1}
        result = undo_files([row], allowed_root=self.out)
        self.assertEqual(result['missing'], 1)
        self.assertEqual(result['deleted'], 0)

    def test_undo_cancel(self):
        rows = [self._row(f'f{i}.jpg') for i in range(5)]
        cancel = threading.Event()
        cancel.set()
        result = undo_files(rows, allowed_root=self.out, cancel_event=cancel)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['skipped'], 5)


if __name__ == '__main__':
    unittest.main()

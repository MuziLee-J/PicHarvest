"""
人工改「识别名称」后，最终文件名必须跟着变。

缺陷（v1.1.1）：TaskManager.set_manual_name 只改 extracted_name / manual_override，
没有让旧的 final_filename 失效；filename_manager.resolve_filenames 里
"manual_override 且 final_filename 非空" 的分支会直接沿用旧名字，
于是复制出来还是改名前的文件名。
"""

from __future__ import annotations

import os
import unittest

from core.task_manager import TaskManager
from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig,
                    Status, TaskConfig)
from tests.helpers import TempTree, build_dataset

TEMPLATE = '*/1440/SAMPLE_NA.jpg'


class ManualNameSyncTest(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.root = os.path.join(self.base, '示例数据', '20250101')
        self.output = os.path.join(self.base, '整理结果')
        build_dataset(self.root)
        self.manager = self.make_manager()
        self.records = self.manager.scan().files

    def make_manager(self) -> TaskManager:
        config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='advanced',
                             template=TEMPLATE),
            naming=NamingConfig(fields=['姓名']),
            ocr=OcrConfig(engine='none'),
            output=OutputConfig(output_dir=self.output, report_formats=[]),
        )
        manager = TaskManager(config, None, None)
        self.addCleanup(manager.close)
        return manager

    # -------------------------------------------------- 场景

    def test_rename_updates_final_filename(self):
        """先把名字定为 张三，再人工改成 李四 —— 最终文件名必须是李四。"""
        record = self.records[0]
        self.manager.set_manual_name(record, '张三')
        self.manager.rebuild_names()
        self.assertIn('张三', record.final_filename)

        self.manager.set_manual_name(record, '李四')
        self.manager.rebuild_names()

        self.assertIn('李四', record.final_filename)
        self.assertNotIn('张三', record.final_filename)

    def test_rename_updates_suggested_filename_immediately(self):
        """改名后不用等 rebuild，界面读到的建议名就该是新的。"""
        record = self.records[0]
        self.manager.set_manual_name(record, '张三')
        self.manager.rebuild_names()

        self.manager.set_manual_name(record, '王五')

        self.assertIn('王五', record.suggested_filename)
        self.assertNotIn('张三', record.suggested_filename)

    def test_explicit_filename_still_wins(self):
        """人工直接写「最终文件名」时，优先级仍高于识别名称。"""
        record = self.records[0]
        self.manager.set_manual_name(record, '张三')
        self.manager.set_final_filename(record, 'CASE001_自定义名字.jpg')
        self.manager.rebuild_names()

        self.assertEqual(record.final_filename, 'CASE001_自定义名字.jpg')

    def test_rename_applies_to_copied_file(self):
        """端到端：改名后复制出来的文件用新名字。"""
        record = self.records[0]
        self.manager.set_manual_name(record, '张三')
        self.manager.rebuild_names()
        self.manager.set_manual_name(record, '李四')

        result = self.manager.execute()

        self.assertGreaterEqual(result.summary.success, 1)
        copied = [name for name in os.listdir(self.output)
                  if name.lower().endswith('.jpg')]
        self.assertTrue(any('李四' in name for name in copied), copied)
        self.assertFalse(any('张三' in name for name in copied), copied)

    def test_skipped_record_keeps_status(self):
        """改名不该把「跳过」的记录又拉回待命名。"""
        record = self.records[0]
        self.manager.skip_record(record, '测试跳过')

        self.manager.set_manual_name(record, '张三')

        self.assertEqual(record.status, Status.SKIPPED)


if __name__ == '__main__':
    unittest.main()

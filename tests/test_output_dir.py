"""
测试：OCR 结束后修改输出目录。

用户在 OCR 跑完才发现目录选错——这时只该重算目标路径，
不能要求重新识别。这里的断言就是这条底线。
"""

from __future__ import annotations

import os
import unittest

from core.task_manager import TaskManager
from models import (ExtractRule, ImageRecord, NamingConfig, OcrConfig, OutputConfig,
                    Status, TaskConfig)
from tests.helpers import TempTree


class TestOutputDirAfterOcr(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

        self.root = os.path.join(self.base, '示例数据')
        self.out1 = os.path.join(self.base, '结果A')
        self.out2 = os.path.join(self.base, '结果B')
        source = os.path.join(self.root, 'CASE001', '1440', 'SAMPLE_NA.jpg')
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, 'wb') as handle:
            handle.write(b'fake')

        self.record = ImageRecord(
            index=1, source_path=source,
            rel_path='CASE001/1440/SAMPLE_NA.jpg', source_folder='CASE001',
            original_name='SAMPLE_NA.jpg', ext='.jpg',
            ocr_text='姓名：张三', ocr_done=True, extracted_name='张三',
            status=Status.READY)

        self.config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='advanced',
                             template='*/1440/SAMPLE_NA.jpg'),
            naming=NamingConfig(fields=['姓名'], keep_source=True),
            ocr=OcrConfig(),
            output=OutputConfig(output_dir=self.out1),
        )
        self.manager = TaskManager(self.config)
        self.manager.records = [self.record]
        self.addCleanup(self.manager.close)
        self.manager.rebuild_names()
        self.first_name = self.record.final_filename

    def test_change_dir_keeps_ocr_result_and_rebuilds_names(self):
        ok, error = self.manager.set_output_dir(self.out2)
        self.assertTrue(ok, error)
        self.assertEqual(self.manager.config.output.output_dir, self.out2)
        self.assertEqual(self.record.final_filename, self.first_name,
                         '换目录不应改变文件名，只改变目标文件夹')
        self.assertTrue(self.record.ocr_done, '识别结果必须保留')
        self.assertEqual(self.record.ocr_text, '姓名：张三')
        self.assertEqual(self.record.extracted_name, '张三')
        self.assertTrue(os.path.isdir(self.out2), '新目录应被自动创建')

    def test_rejects_output_equal_to_root(self):
        ok, error = self.manager.set_output_dir(self.root)
        self.assertFalse(ok)
        self.assertIn('不能与扫描目录相同', error)
        self.assertEqual(self.manager.config.output.output_dir, self.out1,
                         '拒绝时必须保持原目录')

    def test_rejects_empty_path(self):
        ok, _error = self.manager.set_output_dir('   ')
        self.assertFalse(ok)
        self.assertEqual(self.manager.config.output.output_dir, self.out1)

    def test_rejects_existing_file(self):
        file_path = os.path.join(self.base, 'not_a_dir.txt')
        with open(file_path, 'w', encoding='utf-8') as handle:
            handle.write('x')
        ok, error = self.manager.set_output_dir(file_path)
        self.assertFalse(ok)
        self.assertIn('不是文件夹', error)

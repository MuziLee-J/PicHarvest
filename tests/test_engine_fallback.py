"""
测试：子进程 OCR 不可用时的降级记录（P1-4）。

降级本身不是错误，但它改变了行为（无强停、单张超时不断），必须可被 UI 读到。
这里只钉住「记录 + 状态切换」，不启动真实引擎。
"""

from __future__ import annotations

import unittest

from core.task_manager import TaskManager
from models import TaskConfig


class TestDegradeRecord(unittest.TestCase):

    def setUp(self):
        self.manager = TaskManager(TaskConfig())

    def test_default_is_not_degraded(self):
        self.assertEqual(self.manager.degraded_reason, '')
        self.assertFalse(self.manager.uses_process_ocr)

    def test_degrade_records_reason_and_switches_mode(self):
        self.manager._using_process = True
        self.manager._degrade_to_inprocess('模型文件缺失', '启动')

        self.assertIn('启动失败', self.manager.degraded_reason)
        self.assertIn('模型文件缺失', self.manager.degraded_reason)
        self.assertFalse(self.manager.uses_process_ocr)
        self.assertEqual(self.manager.ocr_workers, 1)

    def test_empty_error_still_yields_readable_reason(self):
        self.manager._degrade_to_inprocess(None, '初始化')
        self.assertTrue(self.manager.degraded_reason.startswith('OCR 子进程初始化失败：'))
        self.assertIn('未知原因', self.manager.degraded_reason)


if __name__ == '__main__':
    unittest.main()

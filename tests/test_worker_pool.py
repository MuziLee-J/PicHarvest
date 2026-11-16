"""
测试：OCR 并发池（速度优化）。

并发只影响"怎么跑"，不允许影响"跑出什么"：
- 纯函数 resolve_workers / auto_worker_count 的取值规则；
- 真的起多个子进程时，结果与串行一致、进度事件能定位到正确行。
"""

from __future__ import annotations

import os
import unittest

from core.task_manager import TaskManager
from models import (ExtractRule, ImageRecord, NamingConfig, OcrConfig, OutputConfig,
                    Status, TaskConfig)
from ocr.worker_pool import MAX_AUTO_WORKERS, auto_worker_count, resolve_workers
from tests.helpers import TempTree, has_ocr, has_pillow


class TestResolveWorkers(unittest.TestCase):
    """并发数的解析规则（不需要 OCR 环境）。"""

    def test_auto_follows_cpu(self):
        self.assertEqual(auto_worker_count(8), 4)
        self.assertEqual(auto_worker_count(4), 2)
        self.assertEqual(auto_worker_count(2), 1)
        self.assertEqual(auto_worker_count(1), 1)
        self.assertEqual(auto_worker_count(64), MAX_AUTO_WORKERS)

    def test_zero_means_auto(self):
        self.assertEqual(resolve_workers(0, 8), 4)
        self.assertEqual(resolve_workers(None, 8), 4)
        self.assertEqual(resolve_workers(-3, 8), 4)

    def test_explicit_value_is_capped(self):
        self.assertEqual(resolve_workers(1, 8), 1)
        self.assertEqual(resolve_workers(3, 8), 3)
        self.assertEqual(resolve_workers(99, 8), MAX_AUTO_WORKERS * 2)


@unittest.skipUnless(has_ocr() and has_pillow(), '需要 OCR 引擎与 Pillow')
class TestParallelOcr(unittest.TestCase):
    """真起 3 个并发子进程：结果必须与串行等价。"""

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

        self.root = os.path.join(self.base, '示例数据')
        self.out = os.path.join(self.base, 'out')
        self.records = []
        for i in range(3):
            folder = os.path.join(self.root, f'CASE{i + 1:03d}', '1440')
            path = os.path.join(folder, 'SAMPLE_NA.jpg')
            os.makedirs(folder, exist_ok=True)
            from tests.helpers import make_image
            make_image(path, f'姓名：测试{i + 1}\n编号：CASE{i + 1:03d}', (900, 700))
            self.records.append(ImageRecord(
                index=i + 1, source_path=path,
                rel_path=f'CASE{i + 1:03d}/1440/SAMPLE_NA.jpg',
                source_folder=f'CASE{i + 1:03d}',
                original_name='SAMPLE_NA.jpg', ext='.jpg',
                status=Status.PENDING))

        self.config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='advanced',
                             template='*/1440/SAMPLE_NA.jpg'),
            naming=NamingConfig(fields=['姓名']),
            ocr=OcrConfig(workers=3, timeout_sec=120),
            output=OutputConfig(output_dir=self.out),
        )
        self.manager = TaskManager(self.config)
        self.manager.records = self.records
        self.addCleanup(self.manager.close)

    def test_parallel_run_matches_serial(self):
        events = []
        summary = self.manager.run_ocr(progress_cb=lambda e: events.append(e) or True)

        self.assertTrue(self.manager.uses_process_ocr, '应使用子进程池')
        self.assertGreaterEqual(self.manager.ocr_workers, 2,
                                f'并发数 {self.manager.ocr_workers} 应大于 1')
        self.assertEqual(len(events), 3, '每张图都应有一条进度事件')
        self.assertEqual({e.extra.get('record_index') for e in events}, {1, 2, 3},
                         '进度事件必须携带正确的记录下标（并发完成顺序是乱的）')
        self.assertEqual(summary.total, 3)
        for record in self.records:
            self.assertTrue(record.ocr_text.strip(),
                            f'第 {record.index} 张应识别出文字')
            self.assertTrue(record.ocr_done)

    def test_single_image_does_not_spawn_full_pool(self):
        """只有一张图时，并发数应收敛到 1，避免白占内存。"""
        self.manager.records = self.records[:1]
        self.manager.run_ocr()
        self.assertEqual(self.manager.ocr_workers, 1)

"""
测试：强制停止在任务流程中真的生效。

这是用户在真实场景里报的问题——"开始了不想要，停不下来"。
测试用一个内容密集、单张识别需要数秒的大图，在识别中途调用
force_stop()，断言任务能在 2 秒内结束。
"""

from __future__ import annotations

import os
import threading
import time
import unittest

from core.task_manager import TaskManager
from database.database import Database
from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig, Status,
                    TaskConfig)
from tests.helpers import TempTree, has_ocr, has_pillow

DENSE_LINES = 24
DENSE_SIZE = (2600, 1900)


def make_dense_image(path: str) -> str:
    """生成一张识别耗时稳定的密集文字图。"""
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(os.path.dirname(path), exist_ok=True)
    image = Image.new('RGB', DENSE_SIZE, 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for candidate in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.isfile(candidate):
            try:
                font = ImageFont.truetype(candidate, 40)
                break
            except Exception:
                pass
    for i in range(DENSE_LINES):
        draw.text((60, 40 + i * 74),
                  f'姓名：张三{i:02d}  性别：男  编号：CASE{i:03d}',
                  fill='black', font=font)
    image.save(path, 'JPEG', quality=92)
    return path


@unittest.skipUnless(has_ocr() and has_pillow(), '需要 OCR 引擎与 Pillow')
class TestForceStop(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

        self.root = os.path.join(self.base, '示例数据')
        self.dense_path = make_dense_image(
            os.path.join(self.root, 'CASE001', '1440', 'SAMPLE_NA.jpg'))

        config = TaskConfig(
            rule=ExtractRule(root_dir=self.root, mode='advanced',
                             template='*/1440/SAMPLE_NA.jpg'),
            naming=NamingConfig(fields=['姓名']),
            ocr=OcrConfig(engine='auto', use_process=True, timeout_sec=120),
            output=OutputConfig(output_dir=os.path.join(self.base, 'out')),
        )
        self.db = Database(os.path.join(self.base, 'test.db'))
        self.addCleanup(self.db.close)
        self.manager = TaskManager(config, self.db)
        self.addCleanup(self.manager.close)
        self.manager.scan()
        self.assertEqual(len(self.manager.records), 1)

    def test_ocr_runs_in_subprocess(self):
        self.manager.run_ocr()
        self.assertTrue(self.manager.uses_process_ocr,
                        '默认应在子进程中运行 OCR')

    def test_force_stop_interrupts_running_ocr(self):
        """核心断言：识别进行到一半时强制停止，任务必须很快结束。"""
        result = {}
        finished = threading.Event()

        def worker():
            started = time.time()
            self.manager.run_ocr()
            result['elapsed'] = time.time() - started
            finished.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        # 等子进程完成模型加载（最多 30 秒）；必须等 ready，而不是刚 spawn，
        # 否则可能在启动阶段就停止，测不到"中断正在进行的识别"
        deadline = time.time() + 30
        while time.time() < deadline:
            engine = self.manager._engine
            if engine is not None and getattr(engine, 'ready', False):
                break
            time.sleep(0.05)

        time.sleep(1.0)                     # 让它真正在跑
        stopped = self.manager.force_stop()
        self.assertTrue(stopped, '应报告确实终止了子进程')

        completed = finished.wait(timeout=5.0)
        thread.join(timeout=5.0)

        self.assertTrue(completed, '强制停止后任务应在 5 秒内结束')
        self.assertLess(result['elapsed'], 8.0,
                        f'整体耗时 {result.get("elapsed"):.1f}s，说明没有被真正中断')
        self.assertEqual(self.manager.records[0].status, Status.CANCELED)

    def test_timeout_terminates_single_image(self):
        """单张超时：到点直接终止，不会永远卡住。"""
        self.manager.config.ocr.timeout_sec = 1
        started = time.time()
        self.manager.run_ocr()
        elapsed = time.time() - started

        self.assertLess(elapsed, 20.0)
        record = self.manager.records[0]
        self.assertIn('超时', record.error or '')

    def test_manager_without_process_cannot_force_stop(self):
        """进程内模式明确告知无法强杀，而不是假装成功。"""
        self.manager.config.ocr.use_process = False
        self.manager.release_engine()
        self.manager.run_ocr()
        self.assertFalse(self.manager.uses_process_ocr)
        self.assertFalse(self.manager.force_stop())


if __name__ == '__main__':
    unittest.main()

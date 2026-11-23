"""
测试：OCR 子进程的启动 / 强制停止语义（P2-5）。

两个回归点：
1. 模型加载期间点「强制停止」必须立刻返回失败，而不是等到 startup_timeout；
2. force_stop 的停止位不能被紧随其后的 start() 悄悄清掉。

用例用「假 worker」代替真实 OCR 子进程，所以不依赖 rapidocr，能在本机跑。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest

from ocr.process_engine import ProcessOcrEngine

# 假 worker：先读一行配置，然后一直不吭声（模拟"模型正在加载，很慢"）
SLOW_WORKER = (
    'import sys, time\n'
    'sys.stdin.readline()\n'
    'time.sleep(60)\n'
)

# 假 worker：读完配置立刻宣告就绪，之后保持存活等请求
FAST_WORKER = (
    'import sys, json\n'
    'sys.stdin.readline()\n'
    'print(json.dumps({"type": "ready", "ok": True, "engine": "fake"}), flush=True)\n'
    'for line in sys.stdin:\n'
    '    pass\n'
)


class _FakeWorkerEngine(ProcessOcrEngine):
    """把子进程命令换成一段内联 Python。"""

    worker_source = SLOW_WORKER

    def _worker_command(self):
        return [sys.executable, '-u', '-c', self.worker_source]


class SlowEngine(_FakeWorkerEngine):
    worker_source = SLOW_WORKER


class FastEngine(_FakeWorkerEngine):
    worker_source = FAST_WORKER


class TestStartupStop(unittest.TestCase):

    def test_force_stop_interrupts_startup(self):
        """启动等待期间强停：start() 应立刻返回失败。"""
        engine = SlowEngine(startup_timeout=30)
        self.addCleanup(engine.release)

        outcome: dict = {}

        def _run():
            outcome['value'] = engine.start()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.6)          # 让 start() 真的把子进程拉起来并进入等待
        stopped = engine.force_stop()
        thread.join(5)

        self.assertFalse(thread.is_alive(), 'start() 没被强停唤醒')
        self.assertTrue(stopped, 'force_stop 应该报告"确实杀掉了进程"')
        ok, error = outcome['value']
        self.assertFalse(ok)
        self.assertIn('强制停止', error)
        self.assertFalse(engine.is_alive())

    def test_start_clears_previous_stop_flag(self):
        """强停之后重新 start()：停止位要归零，否则下次识别永远判为已停止。"""
        engine = FastEngine(startup_timeout=10)
        self.addCleanup(engine.release)

        self.assertTrue(engine.force_stop() is False)   # 还没有进程可杀
        self.assertTrue(engine._stopped)

        ok, error = engine.start()
        self.assertTrue(ok, error)
        self.assertFalse(engine._stopped)
        self.assertTrue(engine.ready)

    def test_ready_round_trip_and_stop(self):
        """就绪握手正常，强停后引擎不再存活、ready 为 False。"""
        engine = FastEngine(startup_timeout=10)
        self.addCleanup(engine.release)

        ok, error = engine.start()
        self.assertTrue(ok, error)
        self.assertEqual(engine._engine_label, 'fake')

        self.assertTrue(engine.force_stop())
        self.assertFalse(engine.is_alive())
        self.assertFalse(engine.ready)


if __name__ == '__main__':
    unittest.main()

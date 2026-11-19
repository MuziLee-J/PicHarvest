"""
测试：进程内引擎缓存的键必须包含模型路径（P2-16）。

回归点：以前 auto 分支把引擎写进裸键（'rapid'），于是「用自定义模型跑过一次」
之后，后续普通调用会拿到那颗自定义模型的引擎，模型与配置不一致。
"""

from __future__ import annotations

import unittest
from unittest import mock

from ocr import engine as engine_module


class FakeEngine:
    """够用的假引擎：只实现被缓存逻辑用到的几个成员。"""

    display_name = 'FakeOCR'

    def __init__(self, tag=''):
        self.tag = tag

    @staticmethod
    def is_available() -> bool:
        return True

    def release(self) -> None:
        pass


class TestEngineCacheKey(unittest.TestCase):

    def setUp(self):
        engine_module.reset_engines()
        self.addCleanup(engine_module.reset_engines)
        patcher = mock.patch.object(engine_module, '_ENGINE_ORDER', ('rapid',))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install_factory(self):
        calls = []

        def factory(key, rec_model_path='', det_model_path=''):
            calls.append((key, rec_model_path, det_model_path))
            return FakeEngine(f'{key}:{rec_model_path}:{det_model_path}')

        patcher = mock.patch.object(engine_module, '_create_engine',
                                    side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_custom_model_does_not_leak_into_plain_cache(self):
        calls = self._install_factory()

        custom = engine_module.get_engine('auto', rec_model_path='D:/models/rec')
        plain = engine_module.get_engine('auto')

        self.assertEqual(len(calls), 2, '普通调用复用到了自定义模型的引擎')
        self.assertIsNot(custom, plain)
        self.assertEqual(plain.tag, 'rapid::')

    def test_plain_engine_is_reused(self):
        calls = self._install_factory()

        first = engine_module.get_engine('auto')
        second = engine_module.get_engine('auto')

        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

    def test_same_custom_model_is_recreated(self):
        """带自定义模型时不复用缓存（模型文件可能被原地替换），每次重新加载。

        这是 get_engine 里的既有设计（`force_reload = True`），本用例把它固定下来：
        复用只发生在「没带自定义模型」的普通调用上。
        """
        calls = self._install_factory()

        first = engine_module.get_engine('auto', rec_model_path='D:/m', det_model_path='D:/d')
        second = engine_module.get_engine('auto', rec_model_path='D:/m', det_model_path='D:/d')

        self.assertIsNot(first, second)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()

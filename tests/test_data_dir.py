"""数据目录回退测试（P1-5：数据目录不可写时不要让程序起不来）。"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest import mock

from tests.helpers import TempTree
from utils import file_utils


class DataDirFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(self.tree.__exit__, None, None, None)
        self._cache = file_utils._DATA_DIR_CACHE
        self._note = file_utils._DATA_DIR_NOTE
        file_utils._DATA_DIR_CACHE = ''
        file_utils._DATA_DIR_NOTE = ''
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        file_utils._DATA_DIR_CACHE = self._cache
        file_utils._DATA_DIR_NOTE = self._note

    def test_uses_local_appdata_when_writable(self) -> None:
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': self.root, 'PICHARVEST_DATA_DIR': ''}):
            result = file_utils.get_app_data_dir()
        self.assertEqual(os.path.normcase(result),
                         os.path.normcase(os.path.join(self.root, 'PicHarvest')))
        self.assertEqual(file_utils.data_dir_note(), '')

    def test_falls_back_when_local_appdata_is_a_file(self) -> None:
        blocker = os.path.join(self.root, 'not_a_dir')
        with open(blocker, 'w', encoding='utf-8') as fh:
            fh.write('x')
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': blocker, 'PICHARVEST_DATA_DIR': ''}):
            result = file_utils.get_app_data_dir()
        self.assertTrue(file_utils._is_writable_dir(result))
        self.assertNotEqual(os.path.normcase(result),
                            os.path.normcase(os.path.join(blocker, 'PicHarvest')))
        self.assertIn('无法写入', file_utils.data_dir_note())

    def test_env_override_wins(self) -> None:
        target = os.path.join(self.root, 'custom_data')
        with mock.patch.dict(os.environ, {'PICHARVEST_DATA_DIR': target}):
            result = file_utils.get_app_data_dir()
        self.assertEqual(os.path.normcase(result), os.path.normcase(target))
        self.assertTrue(os.path.isdir(result))

    def test_fallback_result_can_open_sqlite(self) -> None:
        blocker = os.path.join(self.root, 'blocked_env')
        with open(blocker, 'w', encoding='utf-8') as fh:
            fh.write('x')
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': blocker, 'PICHARVEST_DATA_DIR': ''}):
            data_dir = file_utils.get_app_data_dir()
        probe_db = os.path.join(data_dir, 'probe_write_test.db')
        self.addCleanup(lambda: os.path.isfile(probe_db) and os.remove(probe_db))
        con = sqlite3.connect(probe_db)
        con.execute('create table probe (id integer)')
        con.commit()
        con.close()
        self.assertTrue(os.path.isfile(probe_db))


if __name__ == '__main__':
    unittest.main()

"""
测试一 / 二 / 八：扫描器（只读、严格层级、中文路径、容错）。
"""

from __future__ import annotations

import os
import unittest

from core.scanner import ScanError, Scanner, count_only, preview_rule
from tests.helpers import TARGET_NAME, TempTree, write_file

ZH_ROOT = '示例数据'
ZH_SUB = '工单A组'


class TestScanner(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

    # --------------------------------------------------

    def _build(self):
        for folder in ('CASE001', 'CASE002', 'CASE003'):
            write_file(os.path.join(self.root, folder, '1440', TARGET_NAME))
            write_file(os.path.join(self.root, folder, '2880', TARGET_NAME))
        # 干扰项
        write_file(os.path.join(self.root, 'CASE001', '960', TARGET_NAME))
        write_file(os.path.join(self.root, 'CASE001', '1440', 'OTHER.jpg'))
        write_file(os.path.join(self.root, '其他', '子目录', '1440', TARGET_NAME))
        write_file(os.path.join(self.root, 'readme.txt'), b'text')

    def test_scan_1440_only(self):
        """测试1：*/1440/SAMPLE_NA.jpg 只命中 3 个。"""
        self._build()
        result = Scanner(self.root, f'*/1440/{TARGET_NAME}').scan()
        names = sorted(r.source_folder for r in result.files)
        self.assertEqual(names, ['CASE001', 'CASE002', 'CASE003'])
        self.assertEqual(result.count, 3)

    def test_scan_2880_only(self):
        """测试2：切换规则不会错误提取 1440。"""
        self._build()
        result = Scanner(self.root, f'*/2880/{TARGET_NAME}').scan()
        self.assertEqual(result.count, 3)
        for record in result.files:
            self.assertIn('2880', record.rel_path)
            self.assertNotIn('1440', record.rel_path)

    def test_scan_does_not_touch_files(self):
        """扫描必须是纯只读：文件数量与内容都不变。"""
        self._build()
        before = {}
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                path = os.path.join(dirpath, name)
                before[path] = os.path.getsize(path), os.path.getmtime(path)

        Scanner(self.root, f'*/1440/{TARGET_NAME}').scan()

        after = {}
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                path = os.path.join(dirpath, name)
                after[path] = os.path.getsize(path), os.path.getmtime(path)
        self.assertEqual(before, after)

    def test_chinese_path(self):
        """测试8：中文路径正常工作。"""
        root = os.path.join(self.root, ZH_ROOT, '20250101')
        write_file(os.path.join(root, ZH_SUB, '1440', TARGET_NAME))
        write_file(os.path.join(root, ZH_SUB, '2880', TARGET_NAME))
        result = Scanner(root, f'*/1440/{TARGET_NAME}').scan()
        self.assertEqual(result.count, 1)
        self.assertEqual(result.files[0].source_folder, ZH_SUB)
        self.assertTrue(os.path.isfile(result.files[0].source_path))

    def test_rel_path_and_metadata(self):
        self._build()
        result = Scanner(self.root, f'*/1440/{TARGET_NAME}').scan()
        first = result.files[0]
        self.assertEqual(first.rel_path, f'CASE001/1440/{TARGET_NAME}')
        self.assertEqual(first.source_folder, 'CASE001')
        self.assertEqual(first.original_name, TARGET_NAME)
        self.assertEqual(first.ext, '.jpg')
        self.assertGreater(first.size, 0)
        self.assertEqual(first.index, 1)

    def test_only_images_filter(self):
        self._build()
        write_file(os.path.join(self.root, 'CASE001', '1440', 'note.txt'))
        result = Scanner(self.root, '*/1440/*', only_images=True).scan()
        for record in result.files:
            self.assertIn(record.ext, ['.jpg', '.jpeg', '.png', '.bmp'])

        result_all = Scanner(self.root, '*/1440/*', only_images=False).scan()
        self.assertGreater(len(result_all.files), len(result.files))

    def test_missing_root_raises(self):
        with self.assertRaises(ScanError):
            Scanner(os.path.join(self.root, '不存在'), f'*/1440/{TARGET_NAME}').scan()

    def test_empty_result_is_not_error(self):
        self._build()
        result = Scanner(self.root, f'*/7777/{TARGET_NAME}').scan()
        self.assertEqual(result.count, 0)
        self.assertEqual(result.errors, [])

    def test_cancel_stops_scan(self):
        for i in range(40):
            write_file(os.path.join(self.root, f'F{i:04d}', '1440', TARGET_NAME))
        import threading
        cancel = threading.Event()
        cancel.set()
        result = Scanner(self.root, f'*/1440/{TARGET_NAME}').scan(
            cancel_event=cancel)
        self.assertEqual(result.count, 0)

    def test_deep_recursion_limit(self):
        """** 模式下的深度上限保护。"""
        deep = os.path.join(self.root, *[f'L{i}' for i in range(10)],
                            '1440', TARGET_NAME)
        write_file(deep)
        result = Scanner(self.root, f'**/1440/{TARGET_NAME}', max_depth=4).scan()
        self.assertEqual(result.count, 0)
        self.assertTrue(any('递归深度' in error for _path, error in result.errors))

        result_ok = Scanner(self.root, f'**/1440/{TARGET_NAME}', max_depth=20).scan()
        self.assertEqual(result_ok.count, 1)

    def test_preview_and_count_helpers(self):
        self._build()
        paths, error = preview_rule(self.root, f'*/1440/{TARGET_NAME}')
        self.assertEqual(error, '')
        self.assertEqual(len(paths), 3)
        self.assertEqual(count_only(self.root, f'*/1440/{TARGET_NAME}'), 3)

    def test_natural_sort_order(self):
        """排序应为自然序：CASE2 在 CASE10 之前。"""
        for name in ('CASE10', 'CASE2', 'CASE1'):
            write_file(os.path.join(self.root, name, '1440', TARGET_NAME))
        result = Scanner(self.root, f'*/1440/{TARGET_NAME}').scan()
        self.assertEqual([r.source_folder for r in result.files],
                         ['CASE1', 'CASE2', 'CASE10'])


if __name__ == '__main__':
    unittest.main()

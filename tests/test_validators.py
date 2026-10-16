"""
测试：Windows 文件名合法化处理（需求书第十三、十四节）。
"""

from __future__ import annotations

import unittest

from utils.validators import (fit_path_length, is_reserved_name, is_safe_filename,
                              sanitize_filename, sanitize_stem, split_extension,
                              truncate)


class TestSanitizeStem(unittest.TestCase):

    def test_illegal_chars_replaced(self):
        self.assertEqual(sanitize_stem('张三/李四'), '张三_李四')
        self.assertEqual(sanitize_stem('a:b*c?d"e<f>g|h'), 'a_b_c_d_e_f_g_h')
        self.assertEqual(sanitize_stem('张三\\李四'), '张三_李四')

    def test_whitespace_and_control_chars(self):
        self.assertEqual(sanitize_stem('  张三  '), '张三')
        self.assertEqual(sanitize_stem('张\n三'), '张 三')
        self.assertEqual(sanitize_stem('张\t三'), '张 三')
        self.assertEqual(sanitize_stem('张    三'), '张 三')

    def test_trailing_dots_and_spaces_removed(self):
        self.assertEqual(sanitize_stem('张三...'), '张三')
        self.assertEqual(sanitize_stem('张三   '), '张三')
        self.assertEqual(sanitize_stem('.张三.'), '张三')

    def test_fullwidth_colon(self):
        self.assertEqual(sanitize_stem('姓名：张三'), '姓名_张三')

    def test_duplicate_separators_collapsed(self):
        self.assertEqual(sanitize_stem('a//b'), 'a_b')
        self.assertEqual(sanitize_stem('a____b'), 'a_b')
        self.assertEqual(sanitize_stem('_a_'), 'a')

    def test_empty_becomes_fallback(self):
        self.assertEqual(sanitize_stem(''), '未命名')
        self.assertEqual(sanitize_stem('///'), '未命名')
        self.assertEqual(sanitize_stem(None), '未命名')

    def test_reserved_device_names(self):
        self.assertTrue(is_reserved_name('CON'))
        self.assertTrue(is_reserved_name('nul'))
        self.assertTrue(is_reserved_name('COM1'))
        self.assertTrue(is_reserved_name('LPT9.txt'))
        self.assertFalse(is_reserved_name('CONS'))
        self.assertNotEqual(sanitize_stem('CON').upper(), 'CON')

    def test_truncation(self):
        long_name = '张' * 300
        result = sanitize_stem(long_name, max_len=50)
        self.assertEqual(len(result), 50)

    def test_custom_replacement(self):
        self.assertEqual(sanitize_stem('张三/李四', replacement='-'), '张三-李四')
        self.assertEqual(sanitize_stem('张三/李四', replacement=''), '张三李四')


class TestSanitizeFilename(unittest.TestCase):

    def test_extension_preserved(self):
        self.assertEqual(sanitize_filename('张三/李四.jpg'), '张三_李四.jpg')

    def test_split_extension(self):
        self.assertEqual(split_extension('a.b.jpg'), ('a.b', '.jpg'))
        self.assertEqual(split_extension('noext'), ('noext', ''))
        self.assertEqual(split_extension('.gitignore'), ('.gitignore', ''))

    def test_is_safe_filename(self):
        ok, _ = is_safe_filename('CASE001_张三.jpg')
        self.assertTrue(ok)

        ok, reason = is_safe_filename('张三/李四.jpg')
        self.assertFalse(ok)
        self.assertIn('非法字符', reason)

        ok, reason = is_safe_filename('张三.jpg.')
        self.assertFalse(ok)

        ok, reason = is_safe_filename('NUL.jpg')
        self.assertFalse(ok)

        ok, reason = is_safe_filename('')
        self.assertFalse(ok)

    def test_fit_path_length(self):
        long_stem = '名' * 200
        result = fit_path_length(r'D:\整理结果', long_stem + '.jpg', limit=240)
        self.assertLess(len(r'D:\整理结果' + result), 240)
        self.assertTrue(result.endswith('.jpg'))

        # 本来就不长 -> 原样返回
        self.assertEqual(fit_path_length(r'D:\整理结果', '张三.jpg'), '张三.jpg')

    def test_truncate(self):
        self.assertEqual(truncate('abcdef', 3), 'abc')
        self.assertEqual(truncate('ab', 5), 'ab')


if __name__ == '__main__':
    unittest.main()

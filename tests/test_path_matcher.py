"""
测试一 & 二：路径规则匹配。

验证"按层级严格匹配"，而不是"递归找同名文件"。
"""

from __future__ import annotations

import unittest

from core.path_matcher import (PathTemplate, TemplateError, build_template,
                               describe_template_effect, parse_template_to_basic,
                               relativize_template, validate_template)

TARGET = 'SAMPLE_NA.jpg'


class TestPathTemplate(unittest.TestCase):

    def test_basic_single_level_star(self):
        """测试1：*/1440/SAMPLE_NA.jpg 命中所有一级目录下的 1440。"""
        tpl = PathTemplate(f'*/1440/{TARGET}')
        self.assertTrue(tpl.match(f'CASE001/1440/{TARGET}'))
        self.assertTrue(tpl.match(f'CASE002/1440/{TARGET}'))
        self.assertTrue(tpl.match(f'CASE003/1440/{TARGET}'))

    def test_2880_not_matched(self):
        """测试2：1440 规则不能命中 2880 中的图片。"""
        tpl = PathTemplate(f'*/1440/{TARGET}')
        self.assertFalse(tpl.match(f'CASE001/2880/{TARGET}'))
        self.assertFalse(tpl.match(f'CASE003/2880/{TARGET}'))

        tpl2880 = PathTemplate(f'*/2880/{TARGET}')
        self.assertTrue(tpl2880.match(f'CASE001/2880/{TARGET}'))
        self.assertFalse(tpl2880.match(f'CASE001/1440/{TARGET}'))

    def test_deeper_level_not_matched(self):
        """层级不符（多一层）不应命中 —— 这是与"递归搜索文件名"的本质区别。"""
        tpl = PathTemplate(f'*/1440/{TARGET}')
        self.assertFalse(tpl.match(f'其他/子目录/1440/{TARGET}'))
        self.assertFalse(tpl.match(f'CASE001/1440/sub/{TARGET}'))

    def test_filename_must_match_exactly(self):
        tpl = PathTemplate(f'*/1440/{TARGET}')
        self.assertFalse(tpl.match('CASE001/1440/OTHER.jpg'))
        self.assertFalse(tpl.match('CASE001/1440/SAMPLE_NA.png'))

    def test_case_insensitive_on_windows(self):
        tpl = PathTemplate('*/1440/SAMPLE_NA.JPG')
        self.assertTrue(tpl.match(f'case001/1440/{TARGET}'))

    def test_recursive_double_star(self):
        """** 匹配任意层级。"""
        tpl = PathTemplate(f'**/1440/{TARGET}')
        self.assertTrue(tpl.match(f'CASE001/1440/{TARGET}'))
        self.assertTrue(tpl.match(f'a/b/c/1440/{TARGET}'))
        self.assertFalse(tpl.match(f'a/b/c/2880/{TARGET}'))

    def test_double_star_matches_zero_levels(self):
        tpl = PathTemplate(f'**/1440/{TARGET}')
        self.assertTrue(tpl.match(f'1440/{TARGET}'))

    def test_question_mark_and_star_in_filename(self):
        tpl = PathTemplate('*/1440/*.jpg')
        self.assertTrue(tpl.match(f'CASE001/1440/{TARGET}'))
        self.assertTrue(tpl.match('CASE001/1440/ABC.jpg'))
        self.assertFalse(tpl.match('CASE001/1440/ABC.png'))

        tpl2 = PathTemplate('*/1440/SAMPLE*.jpg')
        self.assertTrue(tpl2.match(f'CASE001/1440/{TARGET}'))

    def test_depth_and_properties(self):
        tpl = PathTemplate(f'*/1440/{TARGET}')
        self.assertEqual(tpl.depth, 3)
        self.assertEqual(tpl.max_depth(), 3)
        self.assertFalse(tpl.has_recursive)
        self.assertEqual(tpl.star_indices(), [0])
        self.assertEqual(tpl.literal_at(1), '1440')
        self.assertIsNone(tpl.literal_at(0))

        recursive = PathTemplate(f'**/1440/{TARGET}')
        self.assertTrue(recursive.has_recursive)
        self.assertIsNone(recursive.max_depth())

    def test_windows_separators_are_normalized(self):
        tpl = PathTemplate(f'*\\1440\\{TARGET}')
        self.assertEqual(tpl.raw, f'*/1440/{TARGET}')
        self.assertTrue(tpl.match(f'CASE001\\1440\\{TARGET}'))

    def test_invalid_template(self):
        with self.assertRaises(TemplateError):
            PathTemplate('')

    def test_bracket_in_folder_name_treated_literal(self):
        """没有通配符时按字面量比较，避免 [ ] 被当成字符集。"""
        tpl = PathTemplate(f'*/AB[1]/{TARGET}')
        self.assertTrue(tpl.match(f'X/AB[1]/{TARGET}'))
        self.assertFalse(tpl.match(f'X/ABA/{TARGET}'))


class TestTemplateHelpers(unittest.TestCase):

    def test_build_template_basic_mode(self):
        self.assertEqual(build_template('*', '1440', TARGET),
                         f'*/1440/{TARGET}')
        self.assertEqual(build_template('所有文件夹', '1440', TARGET),
                         f'所有文件夹/1440/{TARGET}')
        self.assertEqual(build_template('', '2880', TARGET),
                         f'*/2880/{TARGET}')

    def test_build_template_advanced_mode(self):
        self.assertEqual(build_template('', '', '', 'advanced',
                                        f'*/1440/{TARGET}'),
                         f'*/1440/{TARGET}')

    def test_parse_template_to_basic(self):
        parsed = parse_template_to_basic(f'*/1440/{TARGET}')
        self.assertEqual(parsed['level1'], '*')
        self.assertEqual(parsed['level2'], '1440')
        self.assertEqual(parsed['file_name'], TARGET)

    def test_validate_template(self):
        ok, _ = validate_template(f'*/1440/{TARGET}')
        self.assertTrue(ok)

        ok, message = validate_template('')
        self.assertFalse(ok)

        ok, message = validate_template('C:/data/1440/a.jpg')
        self.assertFalse(ok)
        self.assertIn('相对路径', message)

        ok, message = validate_template('*/1440/**')
        self.assertFalse(ok)

        ok, _ = validate_template('**/1440/*.jpg')
        self.assertTrue(ok)

    def test_relativize_template(self):
        root = r'D:\示例数据\20250101'
        self.assertEqual(
            relativize_template(f'D:/示例数据/20250101/*/1440/{TARGET}', root),
            f'*/1440/{TARGET}')
        self.assertEqual(
            relativize_template(f'20250101/*/1440/{TARGET}', root),
            f'*/1440/{TARGET}')

    def test_describe_effect(self):
        text = describe_template_effect(f'*/1440/{TARGET}', '20250101')
        self.assertIn('20250101', text)
        self.assertIn('1440', text)
        self.assertIn(TARGET, text)


if __name__ == '__main__':
    unittest.main()

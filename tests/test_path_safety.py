"""
测试：人工文件名的落点安全（P1-1 / P2-7 回归）。

核心断言：
- 手工输入的越界名（多级 ..、绝对路径）必须先被清洗
- 连接符里的路径分隔符与 Windows 非法字符必须被清洗
- 即使记录里被塞进越界名，复制阶段也必须拒绝写到输出目录之外
"""

from __future__ import annotations

import os
import unittest

from core.extractor import Extractor, is_inside_output
from core.filename_manager import (compose_filename, resolve_filenames,
                                   sanitize_manual_name, sanitize_separator)
from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig,
                    TaskConfig)
from reports.report_manager import excel_safe
from tests.helpers import TARGET_NAME, TempTree, make_record, write_file
from utils import file_utils


def make_config(root: str, output: str) -> TaskConfig:
    return TaskConfig(
        rule=ExtractRule(root_dir=root, mode='advanced',
                         template=f'*/1440/{TARGET_NAME}'),
        naming=NamingConfig(fields=['姓名']),
        ocr=OcrConfig(),
        output=OutputConfig(output_dir=output),
    )


class TestSeparator(unittest.TestCase):
    """P2-7：连接符本身也要过清洗。"""

    def test_path_separators_are_dropped(self):
        for raw in ('/', '\\', ':', '|', '*'):
            cleaned = sanitize_separator(raw)
            self.assertNotIn('/', cleaned)
            self.assertNotIn('\\', cleaned)
            self.assertTrue(cleaned)

    def test_normal_separator_kept(self):
        self.assertEqual(sanitize_separator('_'), '_')
        self.assertEqual(sanitize_separator('-'), '-')
        self.assertEqual(sanitize_separator('__'), '__')

    def test_empty_falls_back(self):
        self.assertEqual(sanitize_separator(''), '_')
        self.assertEqual(sanitize_separator('/\\'), '_')
        self.assertEqual(sanitize_separator('   '), '_')

    def test_compose_filename_with_slash_separator(self):
        naming = NamingConfig(separator='/')
        self.assertEqual(compose_filename('张三', 'CASE001', '.jpg', naming),
                         'CASE001_张三.jpg')

    def test_compose_filename_with_backslash_separator(self):
        # 以前这里会抛 re.error: bad escape (end of pattern)
        naming = NamingConfig(separator='\\')
        self.assertEqual(compose_filename('张三', 'CASE001', '.jpg', naming),
                         'CASE001_张三.jpg')


class TestManualName(unittest.TestCase):
    """P1-1：手工名的清洗规则。"""

    def test_relative_escape_is_cleaned(self):
        cleaned, note = sanitize_manual_name(r'..\..\escape.jpg')
        self.assertTrue(note)
        self.assertNotIn('\\', cleaned)
        self.assertNotIn('/', cleaned)
        self.assertTrue(cleaned.lower().endswith('.jpg'))

    def test_absolute_path_is_cleaned(self):
        cleaned, note = sanitize_manual_name('C:/Windows/Temp/abs.jpg')
        self.assertTrue(note)
        for bad in (':', '/', '\\'):
            self.assertNotIn(bad, cleaned)

    def test_plain_name_untouched(self):
        cleaned, note = sanitize_manual_name('CASE001_张三.jpg')
        self.assertEqual(cleaned, 'CASE001_张三.jpg')
        self.assertEqual(note, '')

    def test_empty_input(self):
        self.assertEqual(sanitize_manual_name(''), ('', ''))
        self.assertEqual(sanitize_manual_name('   '), ('', ''))


class _TempCase(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out, exist_ok=True)

    def _source(self, name='a.jpg', content=b'IMAGE-DATA-1234'):
        return write_file(os.path.join(self.root, 'src', name), content)


class TestResolveCleansManualName(_TempCase):
    """P1-1：resolve_filenames 是最后一道清洗关口。"""

    def test_escape_name_is_cleaned_and_noted(self):
        record = make_record(self._source(), source_folder='CASE001')
        record.extracted_name = '张三'
        record.manual_override = True
        record.final_filename = r'..\..\escape.jpg'
        config = make_config(self.root, self.out)

        resolve_filenames([record], self.out, config.naming, config.output)

        self.assertNotIn('\\', record.final_filename)
        self.assertNotIn('/', record.final_filename)
        self.assertEqual(os.path.basename(record.final_filename),
                         record.final_filename)
        self.assertIn('修正', record.conflict_note or '')

    def test_absolute_name_is_cleaned(self):
        record = make_record(self._source(), source_folder='CASE001')
        record.extracted_name = '张三'
        record.manual_override = True
        record.final_filename = 'C:/Windows/Temp/abs.jpg'
        config = make_config(self.root, self.out)

        resolve_filenames([record], self.out, config.naming, config.output)

        self.assertNotIn(':', record.final_filename)
        self.assertNotIn('/', record.final_filename)


class TestCopyGuard(_TempCase):
    """P1-1：即使记录被绕过清洗塞进越界名，复制阶段也要拒绝。"""

    def test_is_inside_output(self):
        self.assertTrue(is_inside_output(os.path.join(self.out, 'a.jpg'),
                                         self.out, 'a.jpg'))
        self.assertFalse(is_inside_output(os.path.join(self.out, r'..\escape.jpg'),
                                          self.out, r'..\escape.jpg'))
        self.assertFalse(is_inside_output(r'C:\Windows\Temp\abs.jpg', self.out,
                                          r'C:\Windows\Temp\abs.jpg'))
        self.assertFalse(is_inside_output(os.path.join(self.out, 'a.jpg'),
                                          self.out, 'sub/a.jpg'))
        self.assertFalse(is_inside_output(os.path.join(self.out, 'a.jpg'), '', 'a.jpg'))

    def test_extractor_refuses_escape(self):
        record = make_record(self._source(), source_folder='CASE001')
        record.extracted_name = '张三'
        record.manual_override = True
        record.final_filename = r'..\escape.jpg'
        config = make_config(self.root, self.out)

        outcome = Extractor(config)._copy_one(record)

        self.assertFalse(outcome.ok)
        self.assertIn('越界', outcome.error)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'escape.jpg')))

    def test_extractor_still_copies_safe_manual_name(self):
        record = make_record(self._source(), source_folder='CASE001')
        record.extracted_name = '张三'
        record.manual_override = True
        record.final_filename = 'my_name.jpg'
        config = make_config(self.root, self.out)

        outcome = Extractor(config)._copy_one(record)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(os.path.isfile(outcome.dest_path))


class TestSamePath(unittest.TestCase):
    """P2-12：输出目录与根目录的判等必须对大小写与分隔符不敏感。"""

    def test_case_and_separator_insensitive(self):
        self.assertTrue(file_utils.same_path('d:/data', 'D:\\Data'))
        self.assertTrue(file_utils.same_path('C:/a/b', 'c:/A/B'))

    def test_dot_segments_are_normalized(self):
        self.assertTrue(file_utils.same_path('C:/x/../x/y', 'C:/x/y'))

    def test_prefix_is_not_equality(self):
        self.assertFalse(file_utils.same_path('C:/out', 'C:/outside'))

    def test_empty_side_is_never_equal(self):
        self.assertFalse(file_utils.same_path('', 'C:/x'))
        self.assertFalse(file_utils.same_path('C:/x', ''))


class TestExcelSafe(unittest.TestCase):
    """P2-8：xlsx 单元格控制字符。"""
    def test_control_chars_removed(self):
        self.assertEqual(excel_safe('a\x0bb\x0c c'), 'ab c')
        self.assertEqual(excel_safe('正常文本'), '正常文本')

    def test_non_string_passthrough(self):
        self.assertEqual(excel_safe(12), 12)
        self.assertIsNone(excel_safe(None))


if __name__ == '__main__':
    unittest.main()

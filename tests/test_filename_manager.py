"""
测试四 / 五 / 六：命名拼装与重名分配。

覆盖：
- 来源文件夹信息保留（前缀 / 后缀 / 连接符 / 来源层级）
- 同一名称多次出现 -> 不同来源前缀 -> 文件名各不相同
- 最终文件名完全相同 -> -1 / -2 / -3
- 目标目录已有文件 -> 绝不覆盖
- 大小写不敏感
"""

from __future__ import annotations

import os
import unittest

from core.filename_manager import (FilenameAllocator, compose_filename,
                                   compute_final_filename, resolve_filenames)
from models import NamingConfig, OutputConfig, Status
from tests.helpers import TARGET_NAME, TempTree, make_record, write_file


class TestNameCompose(unittest.TestCase):

    def test_prefix_default(self):
        naming = NamingConfig()
        self.assertEqual(
            compose_filename('张三', 'CASE001', '.jpg', naming),
            'CASE001_张三.jpg')

    def test_suffix(self):
        naming = NamingConfig(source_position='suffix')
        self.assertEqual(
            compose_filename('张三', 'CASE001', '.jpg', naming),
            '张三_CASE001.jpg')

    def test_custom_separator(self):
        naming = NamingConfig(separator='-')
        self.assertEqual(
            compose_filename('张三', 'CASE001', '.jpg', naming),
            'CASE001-张三.jpg')

    def test_without_source(self):
        naming = NamingConfig(keep_source=False)
        self.assertEqual(compose_filename('张三', 'CASE001', '.jpg', naming),
                         '张三.jpg')

    def test_name_is_sanitized(self):
        naming = NamingConfig()
        self.assertEqual(
            compose_filename('张三/李四', 'CASE001', '.jpg', naming),
            'CASE001_张三_李四.jpg')

    def test_different_sources_stay_unique(self):
        """测试4：OCR 都是"张三"，但来源不同 -> 文件名必须不同。"""
        naming = NamingConfig()
        names = {compose_filename('张三', folder, '.jpg', naming)
                 for folder in ('CASE001', 'CASE002', 'CASE003')}
        self.assertEqual(len(names), 3)
        self.assertIn('CASE001_张三.jpg', names)


class TestFilenameAllocator(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.out = os.path.join(self.root, '输出')
        os.makedirs(self.out, exist_ok=True)

    def test_no_conflict(self):
        allocator = FilenameAllocator()
        result = allocator.allocate(self.out, '张三.jpg')
        self.assertEqual(result.filename, '张三.jpg')
        self.assertFalse(result.conflict)

    def test_existing_file_is_not_overwritten(self):
        """测试6：目标目录已有文件 -> 追加 -1，不覆盖。"""
        write_file(os.path.join(self.out, 'CASE001_张三.jpg'))
        allocator = FilenameAllocator()
        result = allocator.allocate(self.out, 'CASE001_张三.jpg')
        self.assertTrue(result.conflict)
        self.assertEqual(result.filename, 'CASE001_张三-1.jpg')

    def test_sequential_numbering(self):
        """测试5：-1 也存在时 -> -2，依次递增。"""
        write_file(os.path.join(self.out, 'CASE001_张三.jpg'))
        write_file(os.path.join(self.out, 'CASE001_张三-1.jpg'))
        allocator = FilenameAllocator()
        result = allocator.allocate(self.out, 'CASE001_张三.jpg')
        self.assertEqual(result.filename, 'CASE001_张三-2.jpg')

        write_file(os.path.join(self.out, 'CASE001_张三-2.jpg'))
        allocator.refresh()
        result = allocator.allocate(self.out, 'CASE001_张三.jpg')
        self.assertEqual(result.filename, 'CASE001_张三-3.jpg')

    def test_in_task_conflict(self):
        """同一任务内重复分配同名 -> 自动递增。"""
        allocator = FilenameAllocator()
        first = allocator.allocate(self.out, '张三.jpg')
        second = allocator.allocate(self.out, '张三.jpg')
        third = allocator.allocate(self.out, '张三.jpg')
        self.assertEqual([first.filename, second.filename, third.filename],
                         ['张三.jpg', '张三-1.jpg', '张三-2.jpg'])
        self.assertFalse(first.conflict)
        self.assertTrue(second.conflict)
        self.assertTrue(third.conflict)

    def test_case_insensitive(self):
        """Windows 不区分大小写：ABC.jpg 存在时 abc.jpg 必须改名。"""
        write_file(os.path.join(self.out, 'ABC.jpg'))
        allocator = FilenameAllocator(case_insensitive=True)
        result = allocator.allocate(self.out, 'abc.jpg')
        self.assertEqual(result.filename, 'abc-1.jpg')

    def test_skip_policy(self):
        write_file(os.path.join(self.out, '张三.jpg'))
        allocator = FilenameAllocator(policy='skip')
        result = allocator.allocate(self.out, '张三.jpg')
        self.assertTrue(result.skipped)
        self.assertFalse(result.ok)

    def test_release_allows_reuse(self):
        allocator = FilenameAllocator()
        result = allocator.allocate(self.out, '张三.jpg')
        self.assertFalse(allocator.allocate(self.out, '张三.jpg').conflict is False)
        allocator.release(self.out, result.filename)
        again = allocator.allocate(self.out, '张三.jpg')
        self.assertEqual(again.filename, '张三.jpg')

    def test_empty_name(self):
        allocator = FilenameAllocator()
        self.assertFalse(allocator.allocate(self.out, '').ok)


class TestResolveFilenames(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out, exist_ok=True)

    def _records(self, rows):
        records = []
        for i, (folder, name) in enumerate(rows, start=1):
            source = os.path.join(self.root, folder, '1440', TARGET_NAME)
            write_file(source)
            record = make_record(source, index=i, source_folder=folder)
            record.extracted_name = name
            records.append(record)
        return records

    def test_duplicate_names_get_unique_filenames(self):
        """测试4：三个"张三"来自不同来源 -> 三个不同文件名。"""
        records = self._records([('CASE001', '张三'),
                                 ('CASE002', '张三'),
                                 ('CASE003', '张三')])
        naming = NamingConfig()
        resolve_filenames(records, self.out, naming, OutputConfig())
        names = [r.final_filename for r in records]
        self.assertEqual(names, ['CASE001_张三.jpg', 'CASE002_张三.jpg',
                                 'CASE003_张三.jpg'])
        self.assertEqual(len(set(names)), 3)

    def test_same_source_same_name_gets_suffix(self):
        """测试5：完全相同的最终文件名 -> -1 / -2。"""
        records = self._records([('CASE001', '张三'),
                                 ('CASE001', '张三'),
                                 ('CASE001', '张三')])
        naming = NamingConfig()
        resolve_filenames(records, self.out, naming, OutputConfig())
        names = [r.final_filename for r in records]
        self.assertEqual(names, ['CASE001_张三.jpg', 'CASE001_张三-1.jpg',
                                 'CASE001_张三-2.jpg'])
        self.assertTrue(records[1].conflict)
        self.assertTrue(records[2].conflict)

    def test_existing_files_considered(self):
        """测试6：目录中已存在的文件被纳入考虑。"""
        write_file(os.path.join(self.out, 'CASE001_张三.jpg'))
        records = self._records([('CASE001', '张三')])
        resolve_filenames(records, self.out, NamingConfig(), OutputConfig())
        self.assertEqual(records[0].final_filename, 'CASE001_张三-1.jpg')
        self.assertTrue(records[0].conflict)

    def test_manual_override_wins(self):
        records = self._records([('CASE001', '张三')])
        records[0].manual_override = True
        records[0].final_filename = '我指定的名字.jpg'
        resolve_filenames(records, self.out, NamingConfig(), OutputConfig())
        self.assertEqual(records[0].final_filename, '我指定的名字.jpg')

    def test_skipped_records_untouched(self):
        records = self._records([('CASE001', '张三'), ('CASE002', '李四')])
        records[1].status = Status.SKIPPED
        resolve_filenames(records, self.out, NamingConfig(), OutputConfig())
        self.assertEqual(records[1].final_filename, '')

    def test_source_level_option(self):
        """来源层级可配置：默认第一层，可改为第二层。"""
        source = os.path.join(self.root, 'CASE001', '1440', TARGET_NAME)
        write_file(source)
        record = make_record(source, rel_path=f'CASE001/1440/{TARGET_NAME}')
        record.extracted_name = '张三'

        naming = NamingConfig(source_level=1)
        naming.keep_source = True
        # scanner 负责按 source_level 填 source_folder，这里直接验证拼接
        record.source_folder = '1440'
        resolve_filenames([record], self.out, naming, OutputConfig())
        self.assertEqual(record.final_filename, '1440_张三.jpg')


if __name__ == '__main__':
    unittest.main()

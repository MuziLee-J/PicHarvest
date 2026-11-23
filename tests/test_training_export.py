"""
测试：训练导出的坐标基准（P1-3 回归）。

核心断言：
- 导出必须用"与 OCR 相同"的加载函数取图（内部做 EXIF 转正）
- 尺寸与裁剪来自同一张图，不再读 Image.open().size 的"旋转前"尺寸
- 竖拍照片（EXIF Orientation=6）的裁剪结果不出现宽高互换
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from models import ImageRecord, Status
from reports import training_export
from tests.helpers import TempTree, has_pillow


class FakeCrop:

    def __init__(self, box):
        self.box = box
        self.saved = None
        self.closed = False

    def save(self, path, fmt='JPEG', **kwargs):
        with open(path, 'wb') as handle:
            handle.write(b'FAKE-CROP')
        self.saved = path

    def close(self):
        self.closed = True


class FakeImage:
    """伪造一张"已转正"的图，记录调用方要求的裁剪框。"""

    def __init__(self, size=(20, 40)):
        self.size = size
        self.crops = []
        self.closed = False

    def crop(self, box):
        self.crops.append(box)
        return FakeCrop(box)

    def close(self):
        self.closed = True


class TestExportUsesOrientedImage(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

    def _record(self, ocr_size=(20, 40), box=(2, 2, 18, 30)):
        record = ImageRecord(
            index=1,
            source_path=os.path.join(self.root, 'a.jpg'),
            rel_path='CASE001/1440/a.jpg',
            source_folder='CASE001',
            original_name='a.jpg',
            ext='.jpg',
            size=10,
            status=Status.READY,
        )
        record.ocr_text = '姓名：张三'
        record.ocr_lines = ['姓名：张三']
        record.ocr_boxes = [list(box)]
        record.ocr_image_size = ocr_size
        record.extracted_name = '张三'
        record.extract_method = 'manual'
        return record

    def test_never_reads_unoriented_size(self):
        fake = FakeImage()
        record = self._record()
        with mock.patch.object(training_export.file_utils, 'get_image_size',
                               side_effect=AssertionError('不应再读未转正的尺寸')):
            with mock.patch.object(training_export.file_utils, 'load_image_rgb',
                                   return_value=fake):
                stats = training_export.export_training_data([record], self.root)

        self.assertEqual(stats['exported'], 1)
        self.assertEqual(len(fake.crops), 1)
        self.assertTrue(fake.closed)

    def test_size_and_crop_come_from_loaded_image(self):
        fake = FakeImage(size=(20, 40))
        record = self._record(ocr_size=(10, 20), box=(8, 8, 10, 14))
        with mock.patch.object(training_export.file_utils, 'load_image_rgb',
                               return_value=fake):
            stats = training_export.export_training_data([record], self.root)

        self.assertEqual(stats['exported'], 1)
        # ocr 10x20 -> 原图 20x40，比例 2；再各自外扩 6px 并夹到图内
        self.assertEqual(tuple(fake.crops[0]), (10, 10, 20, 34))

    def test_load_failure_is_counted_not_raised(self):
        record = self._record()
        with mock.patch.object(training_export.file_utils, 'load_image_rgb',
                               side_effect=OSError('坏图')):
            stats = training_export.export_training_data([record], self.root)

        self.assertEqual(stats['exported'], 0)
        self.assertEqual(stats['errors'], 1)


@unittest.skipUnless(has_pillow(), '需要 Pillow')
class TestExportWithRealOrientation(unittest.TestCase):
    """真实 JPEG + EXIF Orientation=6：转正后应为竖图 20x40。"""

    def setUp(self):
        self.tree = TempTree()
        self.root = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

    def _write_rotated(self) -> str:
        from PIL import Image

        path = os.path.join(self.root, 'rotated.jpg')
        # 磁盘上 40x20（横躺），EXIF 标记 6 表示显示时应顺时针转 90°
        image = Image.new('RGB', (40, 20), 'white')
        exif = Image.Exif()
        exif[274] = 6
        image.save(path, 'JPEG', quality=95, exif=exif)
        image.close()
        return path

    def test_crop_matches_display_orientation(self):
        from PIL import Image

        source = self._write_rotated()
        record = ImageRecord(index=1, source_path=source,
                             rel_path='CASE001/1440/rotated.jpg',
                             source_folder='CASE001', original_name='rotated.jpg',
                             ext='.jpg', size=10, status=Status.READY)
        record.ocr_text = '姓名：张三'
        record.ocr_lines = ['姓名：张三']
        record.ocr_boxes = [[0, 0, 20, 40]]
        record.ocr_image_size = (20, 40)      # 转正后的尺寸
        record.extracted_name = '张三'
        record.extract_method = 'manual'

        stats = training_export.export_training_data([record], self.root)

        self.assertEqual(stats['exported'], 1)
        crop_path = os.path.join(stats['root'], 'crops', '00001.jpg')
        with Image.open(crop_path) as crop:
            width, height = crop.size
        # 若仍按"旋转前"尺寸计算，这里会得到宽>高
        self.assertGreater(height, width)


if __name__ == '__main__':
    unittest.main()

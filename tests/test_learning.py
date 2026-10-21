"""
测试：纠正记忆与训练数据导出。

这两项是"用已有数据提升效果"的核心：
- 纠正记忆：把人工修正变成下一次的自动判断（安全前提：只认完全相同的 OCR 文本）
- 训练导出：把确认过的姓名行裁出来，生成可微调识别模型的数据集
"""

from __future__ import annotations

import os
import unittest

from core.correction_memory import CorrectionMemory, normalize_key, text_fingerprint
from core.task_manager import TaskManager
from database.database import Database
from models import (ExtractRule, ImageRecord, NamingConfig, OcrConfig, OutputConfig,
                    Status, TaskConfig)
from reports.training_export import export_training_data
from tests.helpers import TempTree, has_pillow, make_image

SAMPLE_TEXT = '姓名：张三\n性别：男\n编号：CASE001'


def make_config(root: str = '', output: str = '') -> TaskConfig:
    return TaskConfig(
        rule=ExtractRule(root_dir=root),
        naming=NamingConfig(fields=['姓名']),
        ocr=OcrConfig(engine='none'),
        output=OutputConfig(output_dir=output),
    )


class TestCorrectionMemoryUnit(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.db = Database(os.path.join(self.base, 'test.db'))
        self.addCleanup(self.db.close)
        self.memory = CorrectionMemory(self.db)

    def test_normalize_ignores_whitespace(self):
        """行内空白不影响匹配；行结构本身保留（不同版面不应混淆）。"""
        self.assertEqual(normalize_key('姓名：张 三\n性别：男'),
                         normalize_key('姓名：张三\r\n  性别:男 '))
        self.assertEqual(text_fingerprint('a b'), text_fingerprint('ab'))
        # 换行位置不同视为不同文本
        self.assertNotEqual(normalize_key('姓名：张三\n性别：男'),
                            normalize_key('姓名：张三 性别：男'))

    def test_remember_and_lookup(self):
        saved = self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁',
                                     'CASE001')
        self.assertTrue(saved)

        hit = self.memory.lookup(SAMPLE_TEXT, '姓名')
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, '张叁')
        self.assertTrue(hit.exact)
        self.assertEqual(hit.confidence, 1.0)

    def test_lookup_is_whitespace_tolerant(self):
        self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁')
        hit = self.memory.lookup('姓名：张三\r\n性别：男\r\n编号：CASE001', '姓名')
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, '张叁')

    def test_unmodified_suggestion_is_not_recorded(self):
        """原样采纳建议不算修正，不应污染记忆。"""
        saved = self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张三')
        self.assertFalse(saved)
        self.assertEqual(self.memory.stats()['count'], 0)

    def test_empty_inputs_ignored(self):
        self.assertFalse(self.memory.remember('', '姓名', '张三', '张叁'))
        self.assertFalse(self.memory.remember(SAMPLE_TEXT, '姓名', '', ''))
        self.assertIsNone(self.memory.lookup(''))

    def test_repeat_correction_increments_count(self):
        self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁')
        self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁')
        hit = self.memory.lookup(SAMPLE_TEXT, '姓名')
        self.assertEqual(hit.use_count, 2)
        self.assertEqual(self.memory.stats()['count'], 1)

    def test_different_document_does_not_match(self):
        """不同文本不得命中 —— 这是刻意不做模糊匹配的原因。"""
        self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁')
        other = '姓名：李四\n性别：男\n编号：CASE001'   # 同一模板、换个人
        self.assertIsNone(self.memory.lookup(other, '姓名'))

    def test_export_csv(self):
        self.memory.remember(SAMPLE_TEXT, '姓名', '张三', '张叁')
        path = os.path.join(self.base, 'corrections.csv')
        self.memory.export_csv(path)
        with open(path, encoding='utf-8-sig') as handle:
            content = handle.read()
        self.assertIn('张叁', content)
        self.assertIn('修正', content)


class TestCorrectionApplied(unittest.TestCase):
    """纠正记忆在任务流程中的生效方式。"""

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))
        self.db = Database(os.path.join(self.base, 'test.db'))
        self.addCleanup(self.db.close)
        self.manager = TaskManager(make_config(), self.db)
        self.addCleanup(self.manager.close)

    def _record(self, text=SAMPLE_TEXT) -> ImageRecord:
        return ImageRecord(index=1, source_path='x.jpg', source_folder='CASE001',
                           original_name='x.jpg', ocr_text=text,
                           status=Status.NEEDS_REVIEW)

    def test_manual_change_is_remembered(self):
        record = self._record()
        record.extracted_name = '张三'          # 系统建议
        self.manager.set_manual_name(record, '张叁')
        self.assertEqual(self.manager.correction_stats()['count'], 1)

        hit = self.manager.correction_memory.lookup(SAMPLE_TEXT, '姓名')
        self.assertEqual(hit.name, '张叁')

    def test_learned_name_takes_priority(self):
        """再次遇到完全相同的 OCR 文本时，历史修正优先于字段识别。"""
        record = self._record()
        record.extracted_name = '张三'
        self.manager.set_manual_name(record, '张叁')

        from ocr.suggester import Suggestion
        from models import ExtractMethod
        original = Suggestion(name='张三', confidence=0.98,
                              method=ExtractMethod.FIELD, reliable=True)
        applied = self.manager._apply_correction_memory(self._record(), original)
        self.assertEqual(applied.name, '张叁')
        self.assertEqual(applied.method, ExtractMethod.LEARNED)
        self.assertTrue(applied.reliable)

    def test_no_memory_keeps_original_suggestion(self):
        from ocr.suggester import Suggestion
        from models import ExtractMethod
        original = Suggestion(name='张三', confidence=0.98,
                              method=ExtractMethod.FIELD, reliable=True)
        applied = self.manager._apply_correction_memory(self._record(), original)
        self.assertEqual(applied.name, '张三')
        self.assertEqual(applied.method, ExtractMethod.FIELD)


@unittest.skipUnless(has_pillow(), '需要 Pillow 才能生成测试图片')
class TestTrainingExport(unittest.TestCase):

    def setUp(self):
        self.tree = TempTree()
        self.base = self.tree.__enter__()
        self.addCleanup(lambda: self.tree.__exit__(None, None, None))

        self.image_path = os.path.join(self.base, 'form.jpg')
        make_image(self.image_path, '姓名：张三\n性别：男', size=(800, 600))

        self.record = ImageRecord(
            index=1,
            source_path=self.image_path,
            source_folder='CASE001',
            original_name='form.jpg',
            ocr_text=SAMPLE_TEXT,
            ocr_lines=['姓名：张三', '性别：男'],
            ocr_boxes=[[60, 70, 260, 130], [60, 150, 200, 200]],
            ocr_image_size=(800, 600),
            ocr_predicted_name='张三',
            extracted_name='张三',
            extract_method='manual',
            status=Status.READY,
        )

    def test_export_creates_crop_and_label(self):
        stats = export_training_data([self.record], self.base, task_id=1)
        self.assertEqual(stats['exported'], 1)

        crop = os.path.join(stats['root'], 'crops', '00001.jpg')
        self.assertTrue(os.path.isfile(crop), '应生成姓名行的截图')

        with open(stats['train_file'], encoding='utf-8') as handle:
            content = handle.read().strip()
        self.assertEqual(content, 'crops/00001.jpg\t姓名：张三')

    def test_corrected_name_is_written_into_label(self):
        """人工把张三改成张叁后，标签应反映修正结果。"""
        self.record.extracted_name = '张叁'
        stats = export_training_data([self.record], self.base, task_id=1)
        with open(stats['train_file'], encoding='utf-8') as handle:
            content = handle.read().strip()
        self.assertIn('姓名：张叁', content)

    def test_box_scaled_when_preprocess_resized(self):
        """预处理缩放后，裁剪框必须映射回原图坐标。"""
        self.record.ocr_image_size = (400, 300)      # 相当于被缩小一半
        stats = export_training_data([self.record], self.base, task_id=1)
        self.assertEqual(stats['exported'], 1)

        import csv
        with open(stats['manifest'], encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        box = [int(v) for v in rows[0]['裁剪框(原图坐标)'].split(',')]
        # 原框 x1=60，预处理图只有原图一半大 -> 映射回原图应为 120 附近
        # （去掉 6px 内边距后是 114）
        self.assertAlmostEqual(box[0], 114, delta=8)
        self.assertGreater(box[2] - box[0], 380)   # 宽度也应被放大

    def test_record_without_name_is_skipped(self):
        self.record.extracted_name = ''
        stats = export_training_data([self.record], self.base, task_id=1)
        self.assertEqual(stats['exported'], 0)
        self.assertEqual(stats['no_line'], 1)

    def test_review_file_lists_every_candidate(self):
        stats = export_training_data([self.record], self.base, task_id=1)
        review = os.path.join(stats['root'], 'review.csv')
        self.assertTrue(os.path.isfile(review))


if __name__ == '__main__':
    unittest.main()

"""
测试三 / 七：OCR 文本解析与命名优先级。

验证需求书第十节的严格 fallback：
    ① 指定字段 → ② AI 判断 → ③ 人工确认
并确认"字段失败时不会随手取 OCR 第一行"。
"""

from __future__ import annotations

import unittest

from models import ExtractMethod, NamingConfig
from ocr.parser import (extract_field, extract_fields, extract_key_values,
                        normalize_text, parse, split_lines)
from ocr.suggester import NameSuggester, score_candidate, suggest_for_text

ID_CARD_TEXT = """中华人民共和国居民身份证
姓名：张三
性别：男
民族：汉
出生：1998年1月1日
住址：北京市朝阳区某某路 1 号
公民身份号码：110101199801010011
"""

CARD_TEXT = """华为技术有限公司
张伟
高级工程师
手机：13800138000
邮箱：zhangwei@example.com
"""

INVOICE_TEXT = """增值税专用发票
发票代码：011002000311
发票号码：12345678
开票日期：2026年10月16日
购买方名称：北京某某科技有限公司
价税合计：￥1000.00
"""


class TestTextNormalize(unittest.TestCase):

    def test_normalize(self):
        text = normalize_text('姓名：张 三\n\n  性别:男  ')
        self.assertEqual(text, '姓名:张 三\n性别:男')

    def test_fullwidth_colon_converted(self):
        """全角冒号统一为半角，便于字段匹配（界面展示仍用 OCR 原文）。"""
        self.assertEqual(normalize_text('姓名：张三'), '姓名:张三')

    def test_split_lines(self):
        self.assertEqual(split_lines('a\n\n b \nc'), ['a', 'b', 'c'])


class TestFieldExtract(unittest.TestCase):

    def test_extract_name_simple(self):
        """测试3：姓名：张三 -> 张三。"""
        hit = extract_field(split_lines(ID_CARD_TEXT), '姓名')
        self.assertTrue(hit.ok)
        self.assertEqual(hit.value, '张三')
        self.assertGreaterEqual(hit.confidence, 0.9)

    def test_extract_with_space_in_label(self):
        hit = extract_field(split_lines('姓 名:李四\n性别:女'), '姓名')
        self.assertTrue(hit.ok)
        self.assertEqual(hit.value, '李四')

    def test_extract_value_on_next_line(self):
        hit = extract_field(split_lines('姓名\n王五\n性别：男'), '姓名')
        self.assertTrue(hit.ok)
        self.assertEqual(hit.value, '王五')

    def test_extract_custom_field(self):
        hit = extract_field(split_lines(INVOICE_TEXT), '日期')
        self.assertTrue(hit.ok)
        self.assertIn('2026', hit.value)

        hit = extract_field(split_lines(INVOICE_TEXT), '购买方名称')
        self.assertTrue(hit.ok)
        self.assertIn('北京', hit.value)

    def test_extract_alias(self):
        """用户写"姓名"，OCR 里是"客户名称"也能命中。"""
        hit = extract_field(split_lines('客户名称：王小明'), '姓名')
        self.assertTrue(hit.ok)
        self.assertEqual(hit.value, '王小明')

    def test_field_not_found(self):
        hit = extract_field(split_lines(INVOICE_TEXT), '姓名')
        self.assertFalse(hit.ok)

    def test_key_values(self):
        values = extract_key_values(split_lines(ID_CARD_TEXT))
        self.assertEqual(values.get('姓名'), '张三')
        self.assertIn('性别', values)

    def test_extract_fields_order(self):
        hits = extract_fields(split_lines(ID_CARD_TEXT), ['姓名', '编号', '地址'])
        self.assertTrue(hits['姓名'].ok)
        self.assertFalse(hits['编号'].ok)          # 身份证文本里没有"编号"
        self.assertTrue(hits['地址'].ok)            # "住址" 是"地址"的别名
        self.assertIn('北京', hits['地址'].value)

    def test_parse_result(self):
        result = parse(ID_CARD_TEXT, ['姓名'])
        self.assertEqual(len(result.lines), 7)
        self.assertTrue(result.fields['姓名'].ok)


class TestSuggesterPriority(unittest.TestCase):
    """命名优先级：字段 → AI → 人工。"""

    def test_field_priority(self):
        """测试3：指定字段命中时直接采用，method=field。"""
        naming = NamingConfig(fields=['姓名'])
        suggestion = suggest_for_text(ID_CARD_TEXT, naming)
        self.assertEqual(suggestion.name, '张三')
        self.assertEqual(suggestion.method, ExtractMethod.FIELD)
        self.assertTrue(suggestion.reliable)

    def test_field_fail_falls_to_ai(self):
        """字段未命中 -> 走 AI 判断（而不是取第一行）。"""
        naming = NamingConfig(fields=['合同编号'], ai_min_confidence=0.4)
        suggestion = suggest_for_text(CARD_TEXT, naming)
        self.assertEqual(suggestion.name, '张伟')
        self.assertEqual(suggestion.method, ExtractMethod.AI)
        self.assertNotIn('华为技术有限公司', suggestion.name)

    def test_ai_does_not_pick_id_card_header(self):
        """没有指定字段时，不能把"中华人民共和国居民身份证"当名字。"""
        naming = NamingConfig(fields=[], ai_min_confidence=0.4)
        suggestion = suggest_for_text(ID_CARD_TEXT, naming)
        self.assertNotEqual(suggestion.name, '中华人民共和国居民身份证')
        self.assertEqual(suggestion.name, '张三')

    def test_ai_rejects_pure_numbers(self):
        """纯数字（手机号/身份证号）不得被当作名称推荐。"""
        naming = NamingConfig(fields=[], ai_min_confidence=0.4)
        suggestion = suggest_for_text('12345678901\n9876543210123456\n￥1,000.00', naming)
        self.assertFalse(suggestion.name)
        self.assertFalse(suggestion.reliable)

    def test_low_confidence_marked_unreliable(self):
        """测试7：无法可靠判断 -> 不可靠（转人工确认）。"""
        naming = NamingConfig(fields=['姓名'], ai_min_confidence=0.9)
        suggestion = suggest_for_text('￥1,000.00\n2026-10-16\n110101199801010011',
                                      naming)
        self.assertFalse(suggestion.reliable)

    def test_empty_text(self):
        naming = NamingConfig(fields=['姓名'])
        suggestion = suggest_for_text('', naming)
        self.assertFalse(suggestion.name)
        self.assertFalse(suggestion.reliable)

    def test_invoice_amount_not_chosen(self):
        naming = NamingConfig(fields=['姓名'], ai_min_confidence=0.4)
        suggestion = suggest_for_text(INVOICE_TEXT, naming)
        self.assertNotIn('1000', suggestion.name or '')
        self.assertNotIn('￥', suggestion.name or '')


class TestScoring(unittest.TestCase):

    def test_chinese_name_scores_high(self):
        score, _ = score_candidate('张三')
        self.assertGreater(score, 4)

    def test_number_line_scores_low(self):
        score, _ = score_candidate('110101199801010011')
        self.assertLess(score, 0)

    def test_date_scores_low(self):
        score, _ = score_candidate('1998年1月1日')
        self.assertLess(score, 0)

    def test_blocklist(self):
        score, reasons = score_candidate('中华人民共和国居民身份证')
        self.assertLess(score, 0)
        self.assertIn('证件固定文本', reasons)

    def test_company_line(self):
        score, _ = score_candidate('北京某某科技有限公司',
                                   {'label': '购买方名称'})
        self.assertGreater(score, 0)


class TestOllamaSafety(unittest.TestCase):

    def test_non_local_url_rejected(self):
        """本地大模型只允许回环地址，杜绝任何外传可能。"""
        from ocr.suggester import OllamaSuggester
        client = OllamaSuggester('http://8.8.8.8:11434')
        self.assertFalse(client.is_local())
        self.assertFalse(client.available())

        local = OllamaSuggester('http://127.0.0.1:11434')
        self.assertTrue(local.is_local())
        local2 = OllamaSuggester('http://localhost:11434')
        self.assertTrue(local2.is_local())


if __name__ == '__main__':
    unittest.main()

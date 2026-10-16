"""
OCR 结果解析：从识别文本中提取"命名字段"（纯标准库，可独立测试）。

支持：
1. 指定字段（含别名）提取，如 "姓名：张三"、"姓 名 张三"、"姓名\n张三"
2. 常见证件/单据模板的关键字段（身份证、名片、合同、发票、学生证…）
3. 行级候选打分（供 suggester 做 AI 判断使用）

设计原则：
- 只做"提取"，不做"猜测式兜底"——兜底交给 ocr/suggester.py，
  严格遵循：指定字段 → AI → 人工确认 的优先级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from utils.validators import (clean_text, collapse_whitespace, count_digits,
                              fullwidth_to_halfwidth, normalize_for_compare)

# ---------------------------------------------------------------- 字段别名库

#: 字段名 -> 常见别名（用户输入"姓名"时这些都会被尝试）
FIELD_ALIASES: dict[str, list[str]] = {
    '姓名': ['姓名', '名字', '姓 名', '名称', '全名', '客户姓名', '客户名称', '收货人',
             '联系人', '申请人', '员工姓名', '患者姓名', '学生姓名', '负责人', '经办人',
             '户名', '签名', 'NAME', 'name'],
    '编号': ['编号', '序号', 'ID', 'ID号', '工号', '学号', '订单号', '订单编号',
             '合同编号', '档案号', '编号码', 'NO', 'No.', 'NO.', 'number'],
    '公司名称': ['公司名称', '公司', '单位名称', '单位', '企业名称', '企业', '机构名称',
                 '供应商名称', '销售方名称', '购买方名称', 'COMPANY'],
    '产品名称': ['产品名称', '品名', '商品名称', '货物名称', '项目名称', '设备名称'],
    '日期': ['日期', '签订日期', '开票日期', '生产日期', '有效期', '截止日期', 'DATE'],
    '合同编号': ['合同编号', '合同号', '协议编号', '协议号'],
    '地址': ['地址', '住址', '联系地址', '通讯地址', 'ADDRESS'],
    '电话': ['电话', '手机', '手机号', '联系电话', 'TEL', 'PHONE'],
    '身份证号': ['身份证号', '身份证号码', '公民身份号码', '证件号码', '身份证件号'],
    '班级': ['班级', '年级', '专业', '学院', '部门', '科室'],
}

#: 明确不应作为"姓名"使用的行（证件/单据固定文本）
BLOCKLIST_LINES = {
    '中华人民共和国居民身份证', '居民身份证', '身份证', '中华人民共和国',
    '性别', '民族', '出生', '出生日期', '住址', '公民身份号码', '签发机关',
    '有效期限', '有效期', '男', '女', '汉', '汉族',
    '签名', '盖章', '备注', '说明', '合计', '总计', '小计', '金额', '合计金额',
    '开票日期', '发票代码', '发票号码', '校验码', '机器编号',
    '购买方', '销售方', '名称', '纳税人识别号', '地址电话', '开户行及账号',
    '价税合计', '收款人', '复核', '开票人',
}

#: 行中若包含这些词，说明是"标签行"而不是"值行"
LABEL_HINTS = ('：', ':', '名称', '编号', '号码', '日期', '地址', '电话', '金额',
               '合计', '备注', '性别', '民族')

#: 字段与值之间的分隔符
_VALUE_SPLIT_RE = re.compile(r'[\s:：=＝\-—>]+')

#: 明显不是人的名字的词
NON_NAME_WORDS = ('公司', '有限', '中心', '医院', '学校', '学院', '大学', '银行', '超市',
                  '商店', '药房', '门诊', '诊所', '工厂', '集团', '事务所', '工作室')


@dataclass
class OcrLine:
    """一行 OCR 文本。"""
    text: str = ''
    confidence: float = 1.0
    index: int = 0

    @property
    def clean(self) -> str:
        return collapse_whitespace(self.text)


@dataclass
class FieldHit:
    """字段提取结果。"""
    field: str = ''            # 用户请求的字段名
    label: str = ''            # 实际命中的标签
    value: str = ''            # 提取到的值
    confidence: float = 0.0    # 0~1
    line_index: int = -1
    reason: str = ''           # 命中说明

    @property
    def ok(self) -> bool:
        return bool(self.value) and self.confidence > 0


@dataclass
class ParseResult:
    """整体解析结果。"""
    text: str = ''
    lines: list = field(default_factory=list)          # list[OcrLine]
    fields: dict = field(default_factory=dict)         # 用户字段 -> FieldHit
    key_values: dict = field(default_factory=dict)     # 所有识别到的 "标签: 值"
    split_text: str = ''                               # 清洗后的整段文本


# ---------------------------------------------------------------- 文本归一化

def normalize_text(raw_text: str) -> str:
    """归一化 OCR 原始文本：全角转半角、统一冒号、压缩空白、去掉竖线噪声。"""
    text = fullwidth_to_halfwidth(raw_text or '')
    text = text.replace('|', ' ').replace('丨', ' ').replace('︱', ' ')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in text.split('\n'):
        line = collapse_whitespace(line)
        if line:
            lines.append(line)
    return '\n'.join(lines)


def split_lines(raw_text: str) -> list[str]:
    """拆分为有效行。"""
    return [line for line in normalize_text(raw_text).split('\n') if line.strip()]


def build_lines(raw_text: str, confidences: list | None = None) -> list[OcrLine]:
    """构造 OcrLine 列表。"""
    lines = split_lines(raw_text)
    result = []
    for i, line in enumerate(lines):
        conf = 1.0
        if confidences and i < len(confidences):
            try:
                conf = float(confidences[i])
            except (TypeError, ValueError):
                conf = 1.0
        result.append(OcrLine(text=line, confidence=conf, index=i))
    return result


def aliases_for(field_name: str) -> list[str]:
    """获取字段的别名列表（用户自定义字段则使用字段本身）。"""
    field_name = (field_name or '').strip()
    if not field_name:
        return []
    aliases = list(FIELD_ALIASES.get(field_name, []))
    if field_name not in aliases:
        aliases.insert(0, field_name)
    # 去掉带空格的写法影响，统一在匹配时处理
    return aliases


# ---------------------------------------------------------------- 单行字段提取

def _trim_value(value: str) -> str:
    """清理字段值：去掉分隔符、括注、尾部噪声。"""
    value = collapse_whitespace(value)
    value = value.strip(' :：=＝-—>、,，;；')
    # 去掉形如 "（签章）" "（必填）" 的括注
    value = re.sub(r'[（(][^）)]{0,8}[）)]\s*$', '', value).strip()
    # 截断到第一个明显的下一个标签
    for sep in ('　', '  '):
        if sep in value:
            value = value.split(sep)[0]
    return value.strip(' :：=＝-—>、,，;；')


def extract_value_from_line(line: str, alias: str) -> str:
    """在一行里寻找 `别名 + 分隔符 + 值` 结构，返回提取到的值。"""
    text = fullwidth_to_halfwidth(line)
    # 允许别名内部出现空格（"姓 名"）
    pattern = r'\s*'.join(re.escape(ch) for ch in alias if not ch.isspace())
    if not pattern:
        return ''
    regex = re.compile(pattern + r'\s*[:：=\-—]{0,3}\s*(.*)$', re.IGNORECASE)
    match = regex.search(text)
    if not match:
        return ''
    tail = _trim_value(match.group(1))
    if not tail:
        return ''
    # 若是 "姓名 性别 男" 这类拼接，取第一段
    tail = _VALUE_SPLIT_RE.split(tail)[0] if ' ' in tail else tail
    return _trim_value(tail)


def looks_like_value(value: str, field_name: str = '') -> bool:
    """判断一段文本像不像该字段的值。"""
    value = (value or '').strip()
    if not value or len(value) > 40:
        return False
    if normalize_for_compare(value) in {normalize_for_compare(w) for w in BLOCKLIST_LINES}:
        return False
    if field_name in ('姓名', '名字'):
        if len(value) < 2 or len(value) > 20:
            return False
        if any(word in value for word in NON_NAME_WORDS):
            return False
        if count_digits(value) > 0:
            return False
        if any(ch in value for ch in ':：/\\'):
            return False
    if field_name in ('日期',) and count_digits(value) < 4:
        return False
    return True


# ---------------------------------------------------------------- 字段提取主流程

def extract_field(lines: list[OcrLine] | list[str], field_name: str) -> FieldHit:
    """按指定字段提取值。

    优先级：同行"标签:值" > 同行"标签 值" > 标签行下一行取值。
    """
    if lines and isinstance(lines[0], str):
        lines = build_lines('\n'.join(lines))     # type: ignore[arg-type]

    aliases = aliases_for(field_name)
    if not aliases:
        return FieldHit(field=field_name, reason='字段名为空')

    # 1) 别名按长度倒序，优先匹配更具体的标签
    for alias in sorted(aliases, key=len, reverse=True):
        for line in lines:
            value = extract_value_from_line(line.text, alias)
            if value and looks_like_value(value, field_name):
                return FieldHit(field=field_name, label=alias, value=value,
                                confidence=0.98 if alias == field_name else 0.92,
                                line_index=line.index,
                                reason=f'命中标签"{alias}"')

    # 2) 标签单独成行，取下一行作为值
    alias_set = {normalize_for_compare(a) for a in aliases}
    for i, line in enumerate(lines):
        norm = normalize_for_compare(line.text)
        if norm in alias_set:
            for follow in lines[i + 1:i + 3]:
                value = _trim_value(follow.text)
                if looks_like_value(value, field_name):
                    return FieldHit(field=field_name, label=line.text.strip(), value=value,
                                    confidence=0.85, line_index=follow.index,
                                    reason='标签与值分两行，取标签下一行')
            break

    # 3) 宽松匹配：整段文本里出现 "别名" 后紧跟值
    for alias in sorted(aliases, key=len, reverse=True):
        pattern = r'\s*'.join(re.escape(ch) for ch in alias if not ch.isspace())
        regex = re.compile(pattern + r'\s*[:：=\-—]{0,3}\s*([^\s:：]{2,20})', re.IGNORECASE)
        for line in lines:
            match = regex.search(fullwidth_to_halfwidth(line.text))
            if match:
                value = _trim_value(match.group(1))
                if looks_like_value(value, field_name):
                    return FieldHit(field=field_name, label=alias, value=value,
                                    confidence=0.8, line_index=line.index,
                                    reason='宽松匹配命中')

    return FieldHit(field=field_name, reason='未找到对应字段')


def extract_fields(lines, field_names: list[str]) -> dict:
    """按用户给定的字段优先级依次提取，返回 {字段名: FieldHit}。"""
    result = {}
    for name in field_names or []:
        name = (name or '').strip()
        if not name:
            continue
        result[name] = extract_field(lines, name)
    return result


def first_success(field_hits: dict, field_order: list[str]) -> FieldHit | None:
    """按用户配置的字段顺序返回第一个成功的结果。"""
    for name in field_order or []:
        hit = field_hits.get(name)
        if hit and hit.ok:
            return hit
    return None


# ---------------------------------------------------------------- 通用键值对

_KV_RE = re.compile(r'^\s*([一-鿿A-Za-z][一-鿿A-Za-z0-9 ]{0,11})'
                    r'\s*[:：]\s*(.{1,60})$')


def extract_key_values(lines: list[OcrLine] | list[str]) -> dict:
    """提取所有 `标签: 值` 结构，用于展示与 AI 打分。"""
    if lines and isinstance(lines[0], str):
        lines = build_lines('\n'.join(lines))     # type: ignore[arg-type]
    result: dict[str, str] = {}
    for line in lines:
        match = _KV_RE.match(fullwidth_to_halfwidth(line.text))
        if not match:
            continue
        key = collapse_whitespace(match.group(1))
        value = _trim_value(match.group(2))
        if key and value and key not in result:
            result[key] = value
    return result


def parse(raw_text: str, field_names: list[str] | None = None,
          confidences: list | None = None) -> ParseResult:
    """完整解析：归一化 + 行拆分 + 字段提取 + 键值对。"""
    lines = build_lines(raw_text, confidences)
    result = ParseResult(
        text=raw_text or '',
        lines=lines,
        split_text='\n'.join(line.text for line in lines),
    )
    result.fields = extract_fields(lines, field_names or [])
    result.key_values = extract_key_values(lines)
    return result

"""
名字建议引擎（纯标准库 + 可选本地 Ollama）。

严格实现需求书第十节的 fallback 优先级：

    ① 用户指定字段   → 成功即采用
    ② AI 判断        → 只在"可靠"时采用
         2a. 本地启发式引擎（默认，零依赖，纯本地）
         2b. 本地 Ollama 大模型（可选开启，强制 127.0.0.1，绝不出本机）
    ③ 人工确认       → 无法可靠判断时交给用户

绝不在字段失败后"随手取 OCR 第一段文字"。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from models import ExtractMethod, NamingConfig
from ocr.parser import (BLOCKLIST_LINES, FIELD_ALIASES, OcrLine, ParseResult,
                        build_lines, normalize_for_compare, _trim_value)
from utils.validators import (clean_text, collapse_whitespace, count_digits,
                              is_mostly_cjk, normalize_for_compare as norm_cmp)

#: 低于该长度的候选通常不是有效名称
MIN_NAME_LEN = 2
#: 超过该长度的候选大概率是整段句子
MAX_NAME_LEN = 24


@dataclass
class Suggestion:
    """名字建议结果。"""
    name: str = ''
    confidence: float = 0.0
    method: str = ''
    reason: str = ''
    reliable: bool = False
    alternatives: list = field(default_factory=list)   # [(候选名, 分数)]

    @property
    def ok(self) -> bool:
        return bool(self.name) and self.reliable


# ==================================================================== 启发式打分

#: 人名/客户名的常见标签（用于加权）
NAME_LABEL_HINTS = ('姓名', '客户', '收货', '联系人', '户名', '申请人', '员工',
                    '患者', '学生', '负责人', '经办人', '签名', '会员', 'name')

#: 商品/公司/项目名的常见标签
ENTITY_LABEL_HINTS = ('产品', '商品', '品名', '货物', '项目', '设备', '公司', '单位',
                      '企业', '机构', '供应商', '销售方', '购买方')

#: 明显是"值"而不是"名字"的模式
BAD_VALUE_PATTERNS = [
    (re.compile(r'^\d{6,}$'), -6.0, '纯数字串'),
    (re.compile(r'^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}'), -6.0, '日期'),
    (re.compile(r'[￥¥$]\s*\d'), -5.0, '金额'),
    (re.compile(r'\d+\s*(元|万元|美元|人民币)'), -5.0, '金额'),
    (re.compile(r'^\d{11}$'), -5.0, '手机号'),
    (re.compile(r'(省|市|区|县|镇|街道|路|号|栋|单元|室)+$'), -4.0, '地址'),
    (re.compile(r'^(http|www\.)', re.I), -5.0, '网址'),
    (re.compile(r'@'), -5.0, '邮箱'),
    (re.compile(r'^\W+$'), -8.0, '无有效字符'),
]

#: 代码/编号类行
_CODE_RE = re.compile(r'^[A-Z0-9\-_/]{6,}$')


def _is_blocklist(text: str) -> bool:
    norm = norm_cmp(collapse_whitespace(text))
    return any(norm == norm_cmp(word) for word in BLOCKLIST_LINES)


def score_candidate(text: str, context: dict | None = None) -> tuple[float, list[str]]:
    """给一个候选文本打分。返回 (分数, 原因列表)。

    分数大致范围 -12 ~ +8；>= 4 视为较可靠。
    """
    context = context or {}
    reasons: list[str] = []
    score = 0.0

    text = _trim_value(collapse_whitespace(text or ''))
    if not text:
        return -99.0, ['空文本']

    length = len(text)
    if length < MIN_NAME_LEN:
        return -99.0, ['过短']
    if length > MAX_NAME_LEN:
        score -= 4.0
        reasons.append('过长')
    else:
        reasons.append(f'长度{length}适中')

    # 长度甜点区（中文人名多为 2-4 字）
    if 2 <= length <= 4:
        score += 4.0
        reasons.append('2-4字，典型中文姓名')
    elif 5 <= length <= 12:
        score += 2.0
    elif 13 <= length <= MAX_NAME_LEN:
        score += 0.5

    # 机构后缀：只有在标签明确指向机构时才该被选中
    if any(word in text for word in ('公司', '有限', '中心', '医院', '学校', '学院',
                                     '大学', '银行', '集团', '事务所')):
        score -= 1.5
        reasons.append('疑似机构名称')

    # 字符构成
    if is_mostly_cjk(text):
        score += 2.0
        reasons.append('以中文为主')
    digits = count_digits(text)
    if digits == 0:
        score += 1.0
    elif digits <= 2:
        score -= 0.5
    else:
        score -= 2.0
        reasons.append('数字偏多')
    letters = sum(1 for ch in text if ch.isalpha() and ord(ch) < 128)
    if letters > 0 and not is_mostly_cjk(text):
        score += 0.5

    # 停用词
    if _is_blocklist(text):
        return -99.0, ['证件固定文本']

    # 标签加权
    norm = norm_cmp(text)
    label_hit = context.get('label', '')
    if label_hit:
        label_norm = norm_cmp(label_hit)
        if any(hint.lower() in label_norm for hint in NAME_LABEL_HINTS):
            score += 4.0
            reasons.append(f'标签"{label_hit}"指向人名')
        elif any(hint.lower() in label_norm for hint in ENTITY_LABEL_HINTS):
            score += 3.0
            reasons.append(f'标签"{label_hit}"指向机构/产品名')
        elif any(hint.lower() in label_norm for hint in
                 ('编号', '号码', 'id', '序号', '代码', '日期', '时间', '电话', '手机')):
            score -= 5.0
            reasons.append(f'标签"{label_hit}"不是名称类字段')

    # 行首/行内标签
    for hint in NAME_LABEL_HINTS:
        if hint in norm:
            score += 3.5
            reasons.append(f'含人名标签"{hint}"')
            break
    else:
        for hint in ENTITY_LABEL_HINTS:
            if hint in norm:
                score += 2.0
                reasons.append(f'含机构/产品标签"{hint}"')
                break

    # 负向模式
    for pattern, penalty, why in BAD_VALUE_PATTERNS:
        if pattern.search(text):
            score += penalty
            reasons.append(f'疑似{why}')

    if _CODE_RE.match(text):
        score -= 3.0
        reasons.append('疑似编号代码')

    # OCR 行本身是多字段拼接
    if text.count(':') + text.count('：') >= 2:
        score -= 2.5
        reasons.append('包含多个字段')

    # OCR 行置信度
    conf = float(context.get('line_confidence', 1.0) or 1.0)
    if conf < 0.6:
        score -= 1.5
        reasons.append('OCR置信度较低')

    return score, reasons


def score_to_confidence(score: float) -> float:
    """把原始分映射到 0~1。"""
    if score <= 0:
        return 0.0
    return round(min(0.98, 0.35 + score / 12.0), 3)


def heuristic_suggest(parse: ParseResult, field_names: list[str] | None = None,
                      min_confidence: float = 0.60) -> Suggestion:
    """本地启发式 AI 判断。"""
    candidates: list[tuple[float, str, list[str]]] = []

    # 1) 键值对候选（带标签，权重最高）
    for key, value in (parse.key_values or {}).items():
        score, reasons = score_candidate(value, {'label': key})
        if score > -50:
            candidates.append((score, value, reasons + [f'来自"{key}"']))

    # 2) 行候选
    for line in parse.lines:
        text = line.text
        # 有标签的行走 KV 逻辑，这里只考虑"裸行"
        if ':' in text or '：' in text:
            head = re.split(r'[:：]', text, maxsplit=1)[0]
            tail = re.split(r'[:：]', text, maxsplit=1)[-1]
            score, reasons = score_candidate(tail, {'label': head,
                                                    'line_confidence': line.confidence})
            if score > -50:
                candidates.append((score, _trim_value(tail), reasons))
            continue
        score, reasons = score_candidate(text, {'line_confidence': line.confidence})
        if score > -50:
            candidates.append((score, text, reasons))

    if not candidates:
        return Suggestion(method=ExtractMethod.AI, reason='没有任何可用候选文本')

    # 去重（同名取最高分）
    best_by_name: dict[str, tuple[float, str, list[str]]] = {}
    for score, name, reasons in candidates:
        key = clean_text(name).lower()
        if not key:
            continue
        if key not in best_by_name or score > best_by_name[key][0]:
            best_by_name[key] = (score, name, reasons)

    ranked = sorted(best_by_name.values(), key=lambda item: -item[0])
    best_score, best_text, best_reasons = ranked[0]
    confidence = score_to_confidence(best_score)
    reliable = confidence >= min_confidence

    # 得分不为正的候选一律不推荐（避免把电话号码、身份证号、整段噪声当名字）
    if best_score <= 0:
        return Suggestion(
            method=ExtractMethod.AI,
            reason='没有出现像名称的候选文本：' + '；'.join(best_reasons[:3]),
            alternatives=[(_finalize_name(name), score_to_confidence(score))
                          for score, name, _ in ranked[:3]
                          if score_to_confidence(score) > 0],
        )

    return Suggestion(
        name=_finalize_name(best_text),
        confidence=confidence,
        method=ExtractMethod.AI,
        reason='；'.join(best_reasons[:4]) if reliable else
               f'置信度不足（{confidence:.2f}）：' + '；'.join(best_reasons[:3]),
        reliable=reliable,
        alternatives=[(_finalize_name(name), score_to_confidence(score))
                      for score, name, _ in ranked[1:5]],
    )


def _finalize_name(text: str) -> str:
    """候选名最后清洗：去掉首尾标点、多余空格。"""
    text = collapse_whitespace(clean_text(text))
    text = text.strip(' .,;:：、，。；-—_()（）【】[]')
    return text


# ==================================================================== 本地大模型（可选）

class OllamaSuggester:
    """可选的本地 Ollama 适配器。

    安全约束：只允许 127.0.0.1 / localhost / ::1，物理上不可能把图片或
    文本发到外网。任何异常都会被吞掉并回退到启发式引擎。
    """

    LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1', '0.0.0.0')

    def __init__(self, url: str = 'http://127.0.0.1:11434',
                 model: str = 'qwen2.5:7b', timeout: int = 30):
        self.url = (url or '').rstrip('/')
        self.model = model
        self.timeout = timeout

    def is_local(self) -> bool:
        try:
            from urllib.parse import urlparse
            host = urlparse(self.url).hostname or ''
            return host in self.LOCAL_HOSTS
        except Exception:
            return False

    def available(self) -> bool:
        """探测本地服务是否可用。"""
        if not self.is_local():
            return False
        try:
            request = urllib.request.Request(f'{self.url}/api/tags', method='GET')
            with urllib.request.urlopen(request, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def suggest(self, parse: ParseResult) -> Suggestion | None:
        """调用本地模型给出建议；失败返回 None。"""
        if not self.is_local():
            return None
        lines = [line.text for line in parse.lines][:60]
        if not lines:
            return None

        prompt = (
            '你是文件命名助手。下面是一张图片的 OCR 文本，请判断最有可能用于'
            '文件命名的关键信息（例如人名、客户名、产品名、公司名）。\n'
            '只输出 JSON，不要解释：{"name":"...", "confidence":0.0-1.0, "reason":"..."}\n'
            '如果无法判断，返回 {"name":"", "confidence":0.0}。\n\n'
            'OCR 文本：\n' + '\n'.join(lines)
        )
        payload = json.dumps({
            'model': self.model,
            'prompt': prompt,
            'stream': False,
            'options': {'temperature': 0.1},
        }).encode('utf-8')

        try:
            request = urllib.request.Request(
                f'{self.url}/api/generate', data=payload,
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode('utf-8', 'ignore'))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        except Exception:
            return None

        raw = (data or {}).get('response', '') or ''
        parsed = _extract_json(raw)
        if not parsed:
            return None
        name = _finalize_name(str(parsed.get('name', '')))
        try:
            confidence = float(parsed.get('confidence', 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not name:
            return None
        return Suggestion(
            name=name,
            confidence=max(0.0, min(0.99, confidence)),
            method=ExtractMethod.AI,
            reason='本地模型判断：' + str(parsed.get('reason', ''))[:120],
            reliable=confidence >= 0.6,
        )


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象。"""
    if not text:
        return None
    match = re.search(r'\{.*\}', text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


# ==================================================================== 统一入口

class NameSuggester:
    """按优先级产生名字建议：指定字段 → AI → （不可靠则人工确认）。"""

    def __init__(self, naming: NamingConfig):
        self.naming = naming
        self._ollama = None
        if getattr(naming, 'ollama_enabled', False):
            self._ollama = OllamaSuggester(naming.ollama_url, naming.ollama_model)

    # --------------------------------------------------

    def suggest(self, raw_text: str, field_hits: dict | None = None,
                confidences: list | None = None) -> Suggestion:
        """主流程。field_hits 为 parser.extract_fields 的结果。"""
        from ocr.parser import parse as parse_text

        parse_result = parse_text(raw_text or '', self.naming.fields, confidences)

        # ---------- ① 指定字段 ----------
        for name in self.naming.fields or []:
            hit = (field_hits or {}).get(name)
            if hit is None:
                hit = parse_result.fields.get(name)
            if hit and hit.ok:
                return Suggestion(name=_finalize_name(hit.value),
                                  confidence=hit.confidence,
                                  method=ExtractMethod.FIELD,
                                  reason=f'指定字段"{name}"命中（{hit.reason}）',
                                  reliable=True)

        if not (raw_text or '').strip():
            return Suggestion(method=ExtractMethod.AI, reason='OCR 文本为空')

        # ---------- ② AI 判断 ----------
        if getattr(self.naming, 'ai_enabled', True):
            min_conf = float(getattr(self.naming, 'ai_min_confidence', 0.60))
            best = heuristic_suggest(parse_result, self.naming.fields, min_conf)

            if self._ollama is not None:
                llm = self._ollama.suggest(parse_result)
                if llm and llm.name and llm.confidence >= best.confidence:
                    llm.reason = llm.reason + f'（启发式候选：{best.name or "无"}）'
                    best = llm

            if best.name and best.reliable:
                return best
            if best.name:
                best.reason = f'AI 判断置信度不足，需人工确认。{best.reason}'
                return best

        # ---------- ③ 交人工确认 ----------
        return Suggestion(method='', reason='无法判断有效名称，已转入人工确认')


# ---------------------------------------------------------------- 便捷函数

def suggest_for_text(raw_text: str, naming: NamingConfig,
                     confidences: list | None = None) -> Suggestion:
    """一次性建议（供测试与命令行使用）。"""
    return NameSuggester(naming).suggest(raw_text, confidences=confidences)


def preview_scoring(text: str) -> list[tuple[str, float, list[str]]]:
    """调试辅助：对多行文本逐行打分。"""
    out = []
    for line in build_lines(text):
        score, reasons = score_candidate(line.text)
        out.append((line.text, score, reasons))
    return out

"""
纠正记忆：把用户每次手工改的名字，变成下一次的自动判断依据。

这是"用已有数据训练"在本项目里最务实的一种形式：
    不需要 GPU、不需要标注工具、几十条修正就能见效，
    而且学到的是**你自己文档的规律**（哪个字段、哪一行才是名字）。

两种匹配：
    精确匹配  同一份 OCR 文本再次出现（同一套模板批量扫描时极常见）
              → 直接采用，置信度 1.0
    模糊匹配  文本只差几个字（同一模板、不同姓名）
              → 作为高于 AI 的建议，阈值默认 0.90

安全：只读取用户在本机做过的修正，不上传任何内容。
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import re
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger('correction')

#: 模糊匹配阈值（越高越保守）
FUZZY_THRESHOLD = 0.90
#: 参与模糊匹配的最大文本长度（超长文本比对开销大且意义不大）
MAX_FUZZY_LEN = 3000
#: 最多比较多少条历史记录
MAX_FUZZY_CANDIDATES = 800

_SPACE_RE = re.compile(r'\s+')


def normalize_key(text: str) -> str:
    """归一化 OCR 文本，作为记忆的键。

    去掉空白、统一全角/半角、转小写 —— 让"同一张表的两张扫描件"能对上。
    """
    from utils.validators import fullwidth_to_halfwidth
    text = fullwidth_to_halfwidth(text or '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 逐行去空白后再拼，避免换行数量不同导致匹配失败
    lines = [re.sub(r'\s+', '', line) for line in text.split('\n')]
    return '\n'.join(line for line in lines if line).lower()


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_key(text).encode('utf-8')).hexdigest()[:32]


@dataclass
class CorrectionHit:
    """命中一条历史修正。"""
    name: str = ''
    field: str = ''
    confidence: float = 0.0
    use_count: int = 0
    similarity: float = 0.0
    exact: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.name)


class CorrectionMemory:
    """纠正记忆（基于 SQLite）。"""

    def __init__(self, database):
        self.db = database

    # ------------------------------------------------------------ 写入

    def remember(self, ocr_text: str, field: str, suggested: str,
                 corrected: str, source_folder: str = '') -> bool:
        """记录一次人工修正。

        只有"确实改了"才记录：如果用户把建议原样采纳，不属于修正。
        """
        corrected = (corrected or '').strip()
        suggested = (suggested or '').strip()
        if not corrected or not ocr_text or not ocr_text.strip():
            return False
        if corrected == suggested:
            return False
        try:
            self.db.add_correction(
                ocr_text=ocr_text,
                field=field or '',
                suggested=suggested,
                corrected=corrected,
                source_folder=source_folder,
                fingerprint=text_fingerprint(ocr_text),
            )
            return True
        except Exception as exc:
            logger.warning('记录修正失败：%s', exc)
            return False

    # ------------------------------------------------------------ 查询

    def lookup(self, ocr_text: str, field: str = '') -> CorrectionHit | None:
        """精确匹配：同一份 OCR 文本之前被修正过。"""
        if not ocr_text or not ocr_text.strip():
            return None
        try:
            row = self.db.get_correction(text_fingerprint(ocr_text), field or '')
        except Exception as exc:
            logger.warning('查询修正记忆失败：%s', exc)
            return None
        if not row:
            return None
        return CorrectionHit(name=row['corrected_name'], field=row['field'],
                             confidence=1.0, use_count=row['use_count'],
                             similarity=1.0, exact=True)

    def lookup_fuzzy(self, ocr_text: str, field: str = '',
                     threshold: float = FUZZY_THRESHOLD) -> CorrectionHit | None:
        """模糊匹配：文本高度相似（同一模板、不同人名）。"""
        if not ocr_text or len(ocr_text) > MAX_FUZZY_LEN:
            return None
        target = normalize_key(ocr_text)
        if len(target) < 20:
            return None

        try:
            rows = self.db.list_corrections(field or '', MAX_FUZZY_CANDIDATES)
        except Exception as exc:
            logger.warning('读取修正记忆失败：%s', exc)
            return None
        if not rows:
            return None

        best = None
        best_ratio = 0.0
        for row in rows:
            candidate = normalize_key(row['ocr_text'])
            if not candidate:
                continue
            # 长度差太大直接跳过，省掉大量比对
            if abs(len(candidate) - len(target)) > max(20, len(target) * 0.3):
                continue
            ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = row

        if best is None or best_ratio < threshold:
            return None
        return CorrectionHit(name=best['corrected_name'], field=best['field'],
                             confidence=round(0.70 + 0.25 * best_ratio, 3),
                             use_count=best['use_count'], similarity=best_ratio,
                             exact=False)

    # ------------------------------------------------------------ 统计 / 导出

    def stats(self) -> dict:
        try:
            return self.db.correction_stats()
        except Exception:
            return {'count': 0, 'fields': {}}

    def export_csv(self, path: str) -> str:
        """导出全部修正记录（可用于人工审阅或作为训练标注参考）。"""
        rows = self.db.list_corrections('', 100000)
        with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['时间', '字段', '来源文件夹', 'OCR文本',
                             '系统建议', '人工修正', '使用次数'])
            for row in rows:
                writer.writerow([
                    row.get('created_at', ''), row.get('field', ''),
                    row.get('source_folder', ''),
                    (row.get('ocr_text', '') or '').replace('\n', ' | '),
                    row.get('suggested_name', ''), row.get('corrected_name', ''),
                    row.get('use_count', 1),
                ])
        return path

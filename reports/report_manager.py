"""
处理报告导出（CSV / JSON / Excel）。

报告字段（需求书第二十七节）：
    序号 / 来源路径 / OCR结果 / 识别名称 / 最终文件名 / 状态 / 错误

额外附带：来源文件夹、提取方式、置信度、目标路径、文件大小
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from typing import Iterable

from models import ImageRecord, Status
from utils import file_utils
from utils.logger import get_logger

logger = get_logger('report')

#: XML/openpyxl 不接受的控制字符（OCR 原文里偶尔会混进 \x0b 之类）
#: 不清理的话 sheet.append 会抛 IllegalCharacterError，整份 xlsx 直接不生成。
_ILLEGAL_XLSX_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def excel_safe(value):
    """把单元格值清理成 openpyxl 可接受的形式。"""
    if not isinstance(value, str):
        return value
    return _ILLEGAL_XLSX_CHARS.sub('', value)

#: 报告列定义：(表头, 记录取值函数)
COLUMNS = [
    ('序号', lambda r: r.index),
    ('来源路径', lambda r: r.source_path),
    ('来源文件夹', lambda r: r.source_folder),
    ('原始文件名', lambda r: r.original_name),
    ('OCR结果', lambda r: (r.ocr_text or '').replace('\n', ' | ')),
    ('识别名称', lambda r: r.extracted_name),
    ('提取方式', lambda r: getattr(r, 'extract_method', '')),
    ('置信度', lambda r: round(r.ocr_confidence, 3) if r.ocr_confidence else ''),
    ('最终文件名', lambda r: r.final_filename or r.suggested_filename),
    ('目标路径', lambda r: r.dest_path),
    ('状态', lambda r: Status.label(r.status)),
    ('重名处理', lambda r: r.conflict_note if r.conflict else ''),
    ('文件大小', lambda r: r.size),
    ('错误', lambda r: r.error or ''),
]


class ReportManager:
    """生成与导出报告。"""

    def __init__(self, output_dir: str, formats: Iterable[str] = ('csv', 'json'),
                 subdir: str = '_reports'):
        self.output_dir = output_dir
        self.formats = [f.lower().strip() for f in (formats or []) if f]
        self.subdir = subdir

    # --------------------------------------------------

    def report_dir(self) -> str:
        path = os.path.join(self.output_dir, self.subdir) if self.subdir else self.output_dir
        return file_utils.ensure_dir(path)

    def build_rows(self, records: list[ImageRecord]) -> list[dict]:
        """构造报告行数据。"""
        rows = []
        for record in records:
            row = {}
            for header, getter in COLUMNS:
                try:
                    row[header] = getter(record)
                except Exception:
                    row[header] = ''
            rows.append(row)
        return rows

    def build_payload(self, records: list[ImageRecord], summary=None,
                      config=None, task_id: int | str = '') -> dict:
        """构造 JSON 报告内容。"""
        payload = {
            '生成时间': time.strftime('%Y-%m-%d %H:%M:%S'),
            '任务编号': task_id,
            '统计': summary.to_dict() if summary is not None else {},
            '配置': config.to_dict() if hasattr(config, 'to_dict') else (config or {}),
            '文件总数': len(records),
            '文件明细': self.build_rows(records),
        }
        return payload

    # --------------------------------------------------

    def export(self, records: list[ImageRecord], summary=None, config=None,
               task_id: int | str = '') -> list[str]:
        """按配置导出所有格式，返回生成的文件路径列表。"""
        if not self.formats:
            return []
        paths: list[str] = []
        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.join(self.report_dir(), f'处理报告_{task_id or "task"}_{stamp}')

        if 'csv' in self.formats:
            path = self._write_csv(records, base + '.csv')
            if path:
                paths.append(path)
        if 'json' in self.formats:
            path = self._write_json(records, summary, config, task_id, base + '.json')
            if path:
                paths.append(path)
        if 'excel' in self.formats or 'xlsx' in self.formats:
            path = self._write_excel(records, summary, base + '.xlsx')
            if path:
                paths.append(path)
        return paths

    # --------------------------------------------------

    def _write_csv(self, records: list[ImageRecord], path: str) -> str | None:
        try:
            file_utils.ensure_dir(os.path.dirname(path))
            # utf-8-sig 让 Excel 直接双击打开不乱码
            with open(file_utils.to_long_path(path), 'w', encoding='utf-8-sig',
                      newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow([header for header, _ in COLUMNS])
                for row in self.build_rows(records):
                    writer.writerow([row.get(header, '') for header, _ in COLUMNS])
            return path
        except OSError as exc:
            logger.error('CSV 报告写入失败：%s', exc)
            return None

    def _write_json(self, records, summary, config, task_id, path: str) -> str | None:
        try:
            file_utils.ensure_dir(os.path.dirname(path))
            payload = self.build_payload(records, summary, config, task_id)
            with open(file_utils.to_long_path(path), 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return path
        except OSError as exc:
            logger.error('JSON 报告写入失败：%s', exc)
            return None

    def _write_excel(self, records, summary, path: str) -> str | None:
        """Excel 报告（需要 openpyxl；未安装时自动跳过）。"""
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
        except ImportError:
            logger.warning('未安装 openpyxl，跳过 Excel 报告')
            return None

        try:
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = '处理明细'

            headers = [header for header, _ in COLUMNS]
            sheet.append(headers)
            header_font = Font(bold=True, color='FFFFFF')
            header_fill = PatternFill('solid', fgColor='3C6E9F')
            for cell in sheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal='center', vertical='center')

            for row in self.build_rows(records):
                sheet.append([excel_safe(row.get(header, '')) for header, _ in COLUMNS])

            widths = [6, 52, 14, 22, 40, 16, 10, 9, 30, 46, 10, 24, 10, 24]
            for i, width in enumerate(widths, start=1):
                sheet.column_dimensions[sheet.cell(row=1, column=i).column_letter].width = width
            sheet.freeze_panes = 'A2'

            if summary is not None:
                info = workbook.create_sheet('任务信息')
                for key, value in (summary.to_dict() if hasattr(summary, 'to_dict')
                                   else dict(summary)).items():
                    info.append([key, value])
                info.column_dimensions['A'].width = 20
                info.column_dimensions['B'].width = 40

            file_utils.ensure_dir(os.path.dirname(path))
            workbook.save(file_utils.to_long_path(path))
            return path
        except Exception as exc:
            logger.error('Excel 报告写入失败：%s', exc)
            return None

    # --------------------------------------------------

    @staticmethod
    def summary_text(summary, records: list[ImageRecord]) -> str:
        """生成人类可读的汇总文本。"""
        lines = [
            f'文件总数：{summary.total}',
            f'成功：{summary.success}',
            f'失败：{summary.failed}',
            f'跳过：{summary.skipped}',
            f'需人工确认：{summary.needs_review}',
            f'重名改名：{summary.conflicts}',
            f'OCR成功：{summary.ocr_ok}',
            f'OCR失败：{summary.ocr_failed}',
            f'耗时：{summary.elapsed_text}',
        ]
        if records:
            ok = sum(1 for r in records if r.status == Status.SUCCESS)
            lines.append(f'实际写入文件：{ok} 个')
        return '\n'.join(lines)

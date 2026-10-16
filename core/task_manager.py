"""
任务编排器：把扫描 → OCR → 命名 → 执行 → 撤销 串成一条可中断的流水线。

设计原则：
- 只依赖 models / core / ocr / database / reports，**不依赖任何 GUI**；
- 通过 progress_cb 回调 + cancel_event/pause_event 与界面交互；
- 扫描与 OCR 阶段只读源文件；只有 Extractor 会写盘；
- 任何单文件异常都不向外抛出，只记录到该记录上。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from core.extractor import Extractor, is_eligible, undo_files
from core.filename_manager import (FilenameAllocator, compute_final_filename,
                                   resolve_filenames)
from core.path_matcher import PathTemplate, describe_template_effect, normalize_template
from core.scanner import Scanner, ScanError
from models import (ExecutionResult, ExtractMethod, ImageRecord, ProgressEvent,
                    ScanResult, Status, TaskConfig, TaskSummary, now_text)
from ocr.engine import OcrUnavailable, get_engine, reset_engines
from ocr.parser import parse as parse_ocr_text
from ocr.suggester import NameSuggester
from utils import file_utils
from utils.logger import get_logger

logger = get_logger('task')

#: OCR 完成后不产生名称时的提示
NO_NAME_HINT = '未能识别出可用名称'


class TaskError(RuntimeError):
    """任务前置条件不满足。"""


class TaskManager:
    """一次整理任务的完整生命周期。"""

    def __init__(self, config: TaskConfig, database=None, task_logger=None):
        self.config = config
        self.db = database
        self.log = task_logger
        self.records: list[ImageRecord] = []
        self.scan_result: ScanResult | None = None
        self.task_id: int | None = None
        self._engine = None
        self._engine_key = ''
        self._lock = threading.RLock()

    # ================================================================ 扫描

    def scan(self,
             progress_cb: Optional[Callable[[int, int, str], bool]] = None,
             cancel_event=None) -> ScanResult:
        """阶段一：只读扫描。绝不修改任何文件。"""
        rule = self.config.rule
        template = self._effective_template()
        scanner = Scanner(
            root_dir=rule.root_dir,
            template=template,
            max_depth=self.config.max_depth,
            only_images=True,
            source_level=self.config.naming.source_level,
        )
        ok, message = scanner.validate()
        if not ok:
            raise TaskError(message)

        if self.log:
            self.log.info(f'开始扫描：根目录={rule.root_dir}，规则={template}')
            self.log.info(describe_template_effect(template, os.path.basename(rule.root_dir)))

        result = scanner.scan(progress_cb=progress_cb, cancel_event=cancel_event)
        self.scan_result = result
        self.records = result.files

        if self.log:
            self.log.info(f'扫描完成：命中 {result.count} 个文件，'
                          f'访问目录 {result.scanned_dirs} 个，'
                          f'耗时 {result.elapsed:.2f} 秒')
            for path, error in result.errors[:20]:
                self.log.warn(f'扫描警告：{path} -> {error}')
        return result

    def _effective_template(self) -> str:
        """根据模式计算最终模板。"""
        rule = self.config.rule
        template = normalize_template(rule.template)
        if rule.mode == 'basic':
            from core.path_matcher import build_template
            template = build_template(rule.level1, rule.level2, rule.file_name, 'basic')
        from core.path_matcher import relativize_template
        return relativize_template(template, rule.root_dir)

    @property
    def template_text(self) -> str:
        return self._effective_template()

    # ================================================================ OCR

    def ensure_engine(self):
        """获取（并按需加载）OCR 引擎。"""
        with self._lock:
            key = self.config.ocr.engine
            if self._engine is None or self._engine_key != key:
                self._engine = get_engine(key)
                self._engine_key = key
            return self._engine

    @property
    def engine_name(self) -> str:
        try:
            return self.ensure_engine().display_name
        except Exception:
            return '未知引擎'

    def release_engine(self) -> None:
        with self._lock:
            self._engine = None
            self._engine_key = ''
        reset_engines()

    def run_ocr(self,
                progress_cb: Optional[Callable[[ProgressEvent], bool]] = None,
                cancel_event=None,
                pause_event=None,
                records: list[ImageRecord] | None = None,
                only: str = '') -> TaskSummary:
        """阶段二：对源图片做本地 OCR 并生成建议名（只读）。

        only: 空=全部；'pending'=只处理未 OCR 的；'failed'=只重试失败的
        """
        targets = self._select_for_ocr(records or self.records, only)
        summary = TaskSummary(started_at=time.time(), total=len(targets), selected=len(targets))

        engine = self.ensure_engine()
        try:
            engine.load()
        except Exception as exc:
            raise TaskError(f'OCR 引擎加载失败：{exc}') from exc

        if self.log:
            self.log.info(f'开始 OCR：引擎={engine.display_name}，待处理 {len(targets)} 张')

        suggester = NameSuggester(self.config.naming)
        cancel_event = cancel_event or threading.Event()
        done = 0

        for record in targets:
            if cancel_event.is_set():
                break
            # 暂停支持
            while pause_event is not None and not pause_event.is_set():
                if cancel_event.is_set():
                    break
                time.sleep(0.15)
            if cancel_event.is_set():
                break

            done += 1
            started = time.time()
            self._ocr_and_name(record, engine, suggester)
            record.ocr_done = record.ocr_done or bool(record.ocr_text)
            elapsed = time.time() - started

            if elapsed > self.config.ocr.timeout_sec:
                if self.log:
                    self.log.warn(f'单张 OCR 耗时 {elapsed:.1f} 秒，超过设定阈值 '
                                  f'{self.config.ocr.timeout_sec} 秒：{record.source_path}')

            if record.ocr_text:
                summary.ocr_ok += 1
            else:
                summary.ocr_failed += 1
            if record.status == Status.NEEDS_REVIEW:
                summary.needs_review += 1

            if progress_cb:
                event = ProgressEvent(
                    phase='ocr', current=done, total=len(targets),
                    message=record.error or record.ocr_error or '',
                    source=record.source_path,
                    ocr_text=record.extracted_name or record.ocr_text[:80],
                    status=record.status,
                    success=summary.ocr_ok, failed=summary.ocr_failed,
                    skipped=summary.skipped,
                    extra={'method': record.extract_method,
                           'confidence': record.ocr_confidence,
                           'elapsed': round(elapsed, 2)},
                )
                try:
                    if progress_cb(event) is False:
                        break
                except Exception:
                    pass

        summary.finished_at = time.time()
        if self.log:
            self.log.info(f'OCR 完成：成功 {summary.ocr_ok}，失败 {summary.ocr_failed}，'
                          f'需人工确认 {summary.needs_review}，耗时 {summary.elapsed_text}')
        return summary

    @staticmethod
    def _select_for_ocr(records: list[ImageRecord], only: str) -> list[ImageRecord]:
        if only == 'pending':
            return [r for r in records if not r.ocr_done]
        if only == 'failed':
            return [r for r in records if not r.ocr_done or r.ocr_error]
        return [r for r in records if r.status != Status.SKIPPED]

    def _ocr_and_name(self, record: ImageRecord, engine, suggester: NameSuggester) -> None:
        """对单个文件执行 OCR + 命名。异常全部内部消化。"""
        record.ocr_error = None
        try:
            image = self._load_for_ocr(record.source_path)
        except Exception as exc:
            record.ocr_error = f'图片读取失败：{exc}'
            record.ocr_text = ''
            record.extracted_name = ''
            record.status = Status.NEEDS_REVIEW
            record.error = record.ocr_error
            self._log_ocr(record)
            return

        try:
            result = engine.recognize(image)
        except Exception as exc:
            record.ocr_error = f'OCR 异常：{exc}'
            record.ocr_text = ''
            record.extracted_name = ''
            record.status = Status.NEEDS_REVIEW
            record.error = record.ocr_error
            self._log_ocr(record)
            return
        finally:
            del image

        record.ocr_engine = result.engine
        record.ocr_confidence = result.avg_confidence
        record.ocr_text = result.text or ''
        record.ocr_error = result.error or None

        # 解析 + 命名（指定字段 → AI → 人工）
        parse_result = parse_ocr_text(record.ocr_text, self.config.naming.fields,
                                      result.scores)
        record.ocr_lines = [line.text for line in parse_result.lines]
        suggestion = suggester.suggest(record.ocr_text,
                                       field_hits=parse_result.fields,
                                       confidences=result.scores)
        self._apply_suggestion(record, suggestion)
        self._log_ocr(record)

    def _apply_suggestion(self, record: ImageRecord, suggestion) -> None:
        """把建议写回记录并决定状态。"""
        name = (suggestion.name or '').strip()
        record.extracted_name = name
        record.extract_method = suggestion.method or ''
        record.extract_reason = suggestion.reason or ''

        if name and suggestion.reliable:
            record.status = Status.READY
            record.error = None
        else:
            record.status = Status.NEEDS_REVIEW
            if record.ocr_error:
                record.error = record.ocr_error
            elif not name:
                record.error = record.ocr_error or NO_NAME_HINT
            else:
                record.error = 'AI 判断置信度不足，请人工确认'

    def _log_ocr(self, record: ImageRecord) -> None:
        if not self.log:
            return
        self.log.info(
            f'OCR[{record.index}] {record.source_path} -> '
            f'引擎={record.ocr_engine or "-"} '
            f'置信度={record.ocr_confidence:.2f} '
            f'方式={record.extract_method or "-"} '
            f'识别名={record.extracted_name or "(空)"} '
            f'状态={Status.label(record.status)}')

    def _load_for_ocr(self, path: str):
        """预处理图片用于 OCR（只读，内存中处理，绝不回写）。"""
        cfg = self.config.ocr
        if cfg.preprocess == 'none':
            try:
                return _preprocess_module().load_array(path, cfg.max_side)
            except Exception:
                return path
        try:
            return _preprocess_module().preprocess(path, cfg.preprocess, cfg.max_side)
        except ImportError:
            # 没有 numpy/Pillow 时直接把路径交给引擎
            return path

    def re_ocr_one(self, record: ImageRecord) -> None:
        """对单张图片重新 OCR（界面"重新OCR"按钮）。"""
        engine = self.ensure_engine()
        engine.load()
        self._ocr_and_name(record, engine, NameSuggester(self.config.naming))

    # ================================================================ 命名

    def set_manual_name(self, record: ImageRecord, name: str) -> None:
        """人工指定名称（采用）。"""
        record.extracted_name = (name or '').strip()
        record.extract_method = ExtractMethod.MANUAL
        record.extract_reason = '人工确认/修改'
        record.manual_override = True
        record.status = Status.READY if record.extracted_name else Status.NEEDS_REVIEW
        if record.status == Status.READY:
            record.error = None

    def set_final_filename(self, record: ImageRecord, filename: str) -> None:
        """人工直接指定最终文件名（优先级最高）。"""
        from utils.validators import is_safe_filename, sanitize_filename
        filename = (filename or '').strip()
        if not filename:
            record.manual_override = False
            return
        safe, reason = is_safe_filename(filename)
        if not safe:
            filename = sanitize_filename(filename, '_', self.config.naming.max_stem_len)
            record.conflict_note = f'文件名已自动修正：{reason}'
        record.final_filename = filename
        record.manual_override = True
        record.status = Status.READY

    def skip_record(self, record: ImageRecord, reason: str = '用户跳过') -> None:
        record.status = Status.SKIPPED
        record.error = reason

    def unskip_record(self, record: ImageRecord) -> None:
        if record.ocr_done:
            record.status = Status.READY if record.extracted_name else Status.NEEDS_REVIEW
        else:
            record.status = Status.PENDING
        record.error = None

    def rebuild_names(self, only_selected: bool = True) -> dict:
        """根据当前 extracted_name / 人工覆盖，重新分配最终文件名。"""
        output_dir = self.config.output.output_dir
        if not output_dir:
            raise TaskError('请先设置输出目录')
        file_utils.ensure_dir(output_dir)
        return resolve_filenames(self.records, output_dir, self.config.naming,
                                 self.config.output, only_selected=only_selected)

    # ================================================================ 执行

    def precheck(self, include_unreviewed: bool = False) -> dict:
        extractor = Extractor(self.config, self.db, self.task_id, self.log)
        return extractor.precheck(self.records, include_unreviewed)

    def execute(self,
                progress_cb: Optional[Callable[[ProgressEvent], bool]] = None,
                cancel_event=None,
                include_unreviewed: bool = False) -> ExecutionResult:
        """阶段三：唯一会写盘的操作。"""
        if not self.config.output.output_dir:
            raise TaskError('请先设置输出目录')

        # 兜底：确保所有记录都有 final_filename
        self.rebuild_names(only_selected=True)

        extractor = Extractor(self.config, self.db, self.task_id, self.log)
        result = extractor.execute(self.records, progress_cb=progress_cb,
                                   cancel_event=cancel_event,
                                   include_unreviewed=include_unreviewed)

        # 把最终统计写回数据库（保证命令行/无界面调用也有完整历史）
        if self.db is not None and self.task_id is not None:
            summary = result.summary
            try:
                self.db.update_task(
                    self.task_id, total=summary.total, success=summary.success,
                    failed=summary.failed, skipped=summary.skipped,
                    needs_review=summary.needs_review, status='finished')
                self.db.mark_finished(self.task_id, 'finished')
            except Exception as exc:
                logger.warning('写回任务统计失败：%s', exc)
        return result

    # ================================================================ 撤销

    def undo(self, task_id: int | None = None,
             progress_cb: Optional[Callable[[int, int, str], bool]] = None,
             cancel_event=None) -> dict:
        """撤销本任务创建的文件（只删目标文件，源文件永不触碰）。"""
        task_id = task_id if task_id is not None else self.task_id
        if task_id is None or self.db is None:
            return {'deleted': 0, 'errors': [('', '没有可撤销的任务')]}

        task = self.db.get_task(task_id)
        allowed_root = (task or {}).get('output_dir', '') or self.config.output.output_dir
        rows = self.db.created_files(task_id)

        if self.log:
            self.log.info(f'开始撤销任务 {task_id}：待删除 {len(rows)} 个由本任务创建的文件')

        result = undo_files(rows, allowed_root=allowed_root,
                            progress_cb=progress_cb, cancel_event=cancel_event)

        # 更新数据库状态
        for row in rows:
            path = row.get('dest_path') or ''
            if path in result['details']:
                try:
                    self.db.update_record_status(task_id, row.get('seq', 0),
                                                 status='undo', created=0,
                                                 error='已被撤销删除')
                except Exception:
                    pass
            # 同步内存中的记录
            for record in self.records:
                if record.dest_path == path:
                    record.status = Status.CANCELED
                    record.created = False
                    record.error = '已被撤销删除'

        if self.log:
            self.log.info(f'撤销完成：删除 {result["deleted"]}，'
                          f'跳过 {result["skipped"]}，失败 {result["failed"]}')
        return result

    # ================================================================ 报告 / 历史

    def export_reports(self, summary=None) -> list[str]:
        """导出报告。"""
        from reports.report_manager import ReportManager

        output = self.config.output
        if not output.output_dir:
            return []
        if summary is None:
            summary = TaskSummary.from_records(self.records)
        manager = ReportManager(output.output_dir, output.report_formats)
        paths = manager.export(self.records, summary=summary, config=self.config,
                               task_id=self.task_id or 'current')
        if self.log:
            for path in paths:
                self.log.info(f'报告已生成：{path}')
        return paths

    def summary(self) -> TaskSummary:
        return TaskSummary.from_records(self.records)

    def statistics(self) -> dict:
        """界面展示用的统计。"""
        summary = self.summary()
        data = summary.to_dict()
        data['template'] = self.template_text
        data['engine'] = self.engine_name
        return data

    def save_to_db(self, status: str = 'scanned') -> int:
        """把当前记录保存到数据库。"""
        if self.db is None:
            return -1
        if self.task_id is None:
            self.task_id = self.db.create_task(
                root_dir=self.config.rule.root_dir,
                template=self.template_text,
                output_dir=self.config.output.output_dir,
                naming_fields=self.config.naming.fields,
                config=self.config.to_dict(),
                log_path=getattr(self.log, 'path', '') if self.log else '')
        self.db.save_records(self.task_id, self.records)
        summary = self.summary()
        self.db.update_task(self.task_id, total=summary.total, success=summary.success,
                            failed=summary.failed, skipped=summary.skipped,
                            needs_review=summary.needs_review, status=status)
        return self.task_id

    def close(self) -> None:
        self.release_engine()
        if self.log:
            self.log.close()


# ---------------------------------------------------------------- 辅助

def _preprocess_module():
    import ocr.preprocess as preprocess
    return preprocess


def quick_pipeline(root_dir: str, template: str, output_dir: str,
                   field_names: list[str] | None = None,
                   limit: int = 0) -> tuple[list[ImageRecord], dict]:
    """无界面的最小闭环（供测试、命令行、CI 使用）。

    只做扫描，不做复制 —— 复制必须由用户显式执行。
    """
    from models import ExtractRule, NamingConfig, OcrConfig, OutputConfig, TaskConfig

    config = TaskConfig(
        rule=ExtractRule(root_dir=root_dir, mode='advanced', template=template),
        naming=NamingConfig(fields=field_names or ['姓名']),
        ocr=OcrConfig(),
        output=OutputConfig(output_dir=output_dir),
    )
    manager = TaskManager(config)
    result = manager.scan()
    records = result.files[:limit] if limit else result.files
    return records, {'count': result.count, 'elapsed': result.elapsed,
                     'errors': result.errors}

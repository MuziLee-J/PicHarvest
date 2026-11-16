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
import queue
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
from ocr.engine import OcrUnavailable, OcrResult, get_engine, reset_engines
from ocr.parser import parse as parse_ocr_text
from ocr.worker_pool import OcrWorkerPool, resolve_workers
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
        self._using_process = False
        self._lock = threading.RLock()
        # 纠正记忆：把用户的人工修正变成下一次的自动判断依据
        self.correction_memory = None
        if database is not None:
            try:
                from core.correction_memory import CorrectionMemory
                self.correction_memory = CorrectionMemory(database)
            except Exception as exc:      # 记忆功能不可用不应影响主流程
                logger.warning('纠正记忆初始化失败：%s', exc)

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

    def ensure_engine(self, limit: int | None = None):
        """获取（并按需加载）OCR 引擎。

        use_process=True 时返回子进程代理引擎 —— 只有它才能被真正强制停止。
        limit: 本次待识别张数。只有一张图时不必拉起 4 个进程白占内存，
               并发数会按实际待处理量收敛。
        """
        with self._lock:
            cfg = self.config.ocr
            wanted = resolve_workers(cfg.workers)
            if limit is not None and limit > 0:
                wanted = min(wanted, int(limit))
            wanted = max(1, wanted)
            key = (cfg.engine, cfg.use_process, cfg.rec_model_path,
                   cfg.det_model_path, cfg.startup_timeout, wanted)
            if self._engine is not None and self._engine_key == key:
                return self._engine

            self._engine = None
            self._engine_key = key
            self._using_process = False

            if cfg.use_process and cfg.engine != 'none':
                try:
                    if wanted > 1:
                        self._engine = OcrWorkerPool(
                            wanted,
                            prefer=cfg.engine,
                            rec_model_path=cfg.rec_model_path,
                            det_model_path=cfg.det_model_path,
                            startup_timeout=cfg.startup_timeout)
                    else:
                        from ocr.process_engine import ProcessOcrEngine
                        self._engine = ProcessOcrEngine(
                            prefer=cfg.engine,
                            rec_model_path=cfg.rec_model_path,
                            det_model_path=cfg.det_model_path,
                            startup_timeout=cfg.startup_timeout)
                    self._using_process = True
                    return self._engine
                except Exception as exc:
                    logger.warning('子进程 OCR 初始化失败，退回进程内模式：%s', exc)

            self._engine = get_engine(
                cfg.engine, rec_model_path=cfg.rec_model_path,
                det_model_path=cfg.det_model_path)
            return self._engine

    @property
    def ocr_workers(self) -> int:
        """当前实际的 OCR 并发数（进程内模式恒为 1）。"""
        if not self._using_process:
            return 1
        return int(getattr(self._engine, 'size', 1) or 1)

    @property
    def engine_name(self) -> str:
        try:
            engine = self.ensure_engine()
            if self._using_process:
                label = getattr(engine, 'engine_label', engine.display_name)
                workers = self.ocr_workers
                if workers > 1:
                    return f'{label}（{workers} 个并发子进程）'
                return f'{label}（子进程）'
            return engine.display_name
        except Exception:
            return '未知引擎'

    @property
    def uses_process_ocr(self) -> bool:
        return self._using_process

    def force_stop(self) -> bool:
        """强制停止正在进行的识别。

        对子进程引擎：直接 kill，正在跑的推理立即结束。
        返回是否确实终止了子进程（进程内模式无法强杀，返回 False）。
        """
        engine = self._engine
        if engine is not None and hasattr(engine, 'force_stop'):
            try:
                stopped = engine.force_stop()
                if self.log:
                    self.log.warn('已强制停止 OCR 子进程' if stopped
                                  else '强制停止：当前没有正在运行的 OCR 子进程')
                return bool(stopped)
            except Exception as exc:
                logger.warning('强制停止失败：%s', exc)
        if self.log:
            self.log.warn('当前为进程内 OCR 模式，无法强制终止正在进行的单张识别')
        return False

    def release_engine(self) -> None:
        with self._lock:
            engine = self._engine
            self._engine = None
            self._engine_key = ''
            self._using_process = False
        try:
            if engine is not None:
                engine.release()
        except Exception:
            pass
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

        engine = self.ensure_engine(limit=len(targets) or None)

        # 子进程模式：启动并等待模型就绪；失败则退回进程内模式
        if self.uses_process_ocr:
            if self.log:
                self.log.info('正在启动 OCR 子进程（首次需加载模型，请稍候）…')
            ok, error = engine.start()
            if not ok:
                if self.log:
                    self.log.warn(f'OCR 子进程启动失败，改用进程内模式：{error}')
                cfg = self.config.ocr
                self._engine = get_engine(cfg.engine,
                                          rec_model_path=cfg.rec_model_path,
                                          det_model_path=cfg.det_model_path)
                self._using_process = False
                engine = self._engine

        if not self.uses_process_ocr:
            try:
                engine.load()
            except Exception as exc:
                raise TaskError(f'OCR 引擎加载失败：{exc}') from exc

        if self.log:
            self.log.info(f'开始 OCR：引擎={self.engine_name}，待处理 {len(targets)} 张')
            if self.uses_process_ocr:
                self.log.info('已启用子进程隔离：可随时强制停止，单张超时会自动终止')

        suggester = NameSuggester(self.config.naming)
        cancel_event = cancel_event or threading.Event()

        workers = self.ocr_workers if self.uses_process_ocr else 1
        if workers > 1 and len(targets) > 1:
            if self.log:
                self.log.info(f'并发识别：{workers} 个子进程同时处理 {len(targets)} 张图片')
            self._run_ocr_parallel(targets, engine, suggester, summary,
                                   progress_cb, cancel_event, pause_event, workers)
        else:
            self._run_ocr_serial(targets, engine, suggester, summary,
                                 progress_cb, cancel_event, pause_event)

        summary.finished_at = time.time()
        if self.log:
            self.log.info(f'OCR 完成：成功 {summary.ocr_ok}，失败 {summary.ocr_failed}，'
                          f'需人工确认 {summary.needs_review}，耗时 {summary.elapsed_text}')
        return summary

    # ---------------------------------------------------------------- 串行 / 并发

    def _run_ocr_serial(self, targets, engine, suggester, summary,
                        progress_cb, cancel_event, pause_event) -> None:
        """串行识别（进程内模式，或用户把并发设为 1）。"""
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
            self._ocr_and_name(record, engine, suggester, cancel_event)
            record.ocr_done = record.ocr_done or bool(record.ocr_text)
            elapsed = time.time() - started

            if record.status == Status.CANCELED:
                if self.log:
                    self.log.warn('识别已被用户强制停止，剩余文件保持未处理状态')
                break

            if elapsed > self.config.ocr.timeout_sec and not self.uses_process_ocr:
                if self.log:
                    self.log.warn(f'单张 OCR 耗时 {elapsed:.1f} 秒，超过设定阈值 '
                                  f'{self.config.ocr.timeout_sec} 秒：{record.source_path}')

            self._account_result(record, summary)
            if not self._emit_ocr_progress(record, summary, done, len(targets),
                                           elapsed, progress_cb):
                break

    def _run_ocr_parallel(self, targets, engine, suggester, summary,
                          progress_cb, cancel_event, pause_event, workers) -> None:
        """并发识别：工作线程只做识别，主线程统一命名与计数。

        为什么要分两段：命名要读写 correction_memory / suggester / 记录状态，
        这些都不是线程安全的；把"重活（识别）"放线程里、"轻活（落库+命名）"
        收回到主线程，既拿到并发，又不用给整条链路加锁。

        取消：一置 cancel_event 就 kill 所有子进程，在途识别立即返回 canceled，
        所以不会出现"点了停止还要等一张图跑完"。
        """
        total = len(targets)
        pending = list(targets)
        guard = threading.Lock()
        results: queue.Queue = queue.Queue()
        stop = threading.Event()
        started = 0            # 已被取走的记录数（可能大于已完成的）
        aborted = False        # progress_cb 返回 False：不再取新记录
        warned_canceled = False

        def take():
            nonlocal started
            with guard:
                if stop.is_set() or cancel_event.is_set() or not pending:
                    return None
                record = pending.pop(0)
                started += 1
                return record

        def worker() -> None:
            while True:
                record = take()
                if record is None:
                    return
                while pause_event is not None and not pause_event.is_set():
                    if cancel_event.is_set() or stop.is_set():
                        break
                    time.sleep(0.1)
                if cancel_event.is_set() or stop.is_set():
                    results.put((record, OcrResult(error='已取消', canceled=True), 0.0))
                    continue
                began = time.time()
                result = self._recognize(record, engine, cancel_event)
                results.put((record, result, time.time() - began))

        threads = [threading.Thread(target=worker, daemon=True, name=f'ocr-worker-{i + 1}')
                   for i in range(max(1, min(workers, total)))]
        for thread in threads:
            thread.start()

        done = 0
        try:
            while True:
                if done >= started and not any(t.is_alive() for t in threads):
                    break
                try:
                    record, result, elapsed = results.get(timeout=0.15)
                except queue.Empty:
                    if (cancel_event.is_set() or aborted) and not stop.is_set():
                        stop.set()
                        self.force_stop()
                    continue

                done += 1
                self._apply_result(record, result, suggester)
                record.ocr_done = record.ocr_done or bool(record.ocr_text)
                self._account_result(record, summary)

                if record.status == Status.CANCELED and not warned_canceled:
                    warned_canceled = True
                    if self.log:
                        self.log.warn('识别已被用户强制停止，剩余文件保持未处理状态')

                if not self._emit_ocr_progress(record, summary, done, total,
                                               elapsed, progress_cb):
                    aborted = True

                if (cancel_event.is_set() or aborted) and not stop.is_set():
                    stop.set()
                    self.force_stop()
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)

    def _emit_ocr_progress(self, record, summary, done, total, elapsed,
                           progress_cb) -> bool:
        """发一条进度事件；返回 False 表示调用方要求中止。"""
        if not progress_cb:
            return True
        event = ProgressEvent(
            phase='ocr', current=done, total=total,
            message=record.error or record.ocr_error or '',
            source=record.source_path,
            ocr_text=record.extracted_name or record.ocr_text[:80],
            status=record.status,
            success=summary.ocr_ok, failed=summary.ocr_failed,
            skipped=summary.skipped,
            # record_index 让并发的乱序结果也能定位到正确的表格行
            extra={'method': record.extract_method,
                   'confidence': record.ocr_confidence,
                   'elapsed': round(elapsed, 2),
                   'record_index': record.index},
        )
        try:
            return progress_cb(event) is not False
        except Exception:
            return True

    @staticmethod
    def _account_result(record: ImageRecord, summary: TaskSummary) -> None:
        if record.ocr_text:
            summary.ocr_ok += 1
        else:
            summary.ocr_failed += 1
        if record.status == Status.NEEDS_REVIEW:
            summary.needs_review += 1

    @staticmethod
    def _select_for_ocr(records: list[ImageRecord], only: str) -> list[ImageRecord]:
        if only == 'pending':
            return [r for r in records if not r.ocr_done]
        if only == 'failed':
            return [r for r in records if not r.ocr_done or r.ocr_error]
        return [r for r in records if r.status != Status.SKIPPED]

    def _ocr_and_name(self, record: ImageRecord, engine, suggester: NameSuggester,
                      cancel_event=None) -> None:
        """串行路径：识别 + 命名。异常全部内部消化。

        注意：识别走 recognize_path()，子进程引擎会把整个推理放进子进程，
        因此 cancel_event 一置位就能立刻杀死进程，不必等当前图片跑完。
        """
        result = self._recognize(record, engine, cancel_event)
        self._apply_result(record, result, suggester)

    def _recognize(self, record: ImageRecord, engine,
                   cancel_event=None) -> OcrResult:
        """只做识别（并发时会被多个线程同时调用，不能碰任何共享状态）。"""
        cfg = self.config.ocr
        try:
            return engine.recognize_path(
                record.source_path,
                preset=cfg.preprocess,
                max_side=cfg.max_side,
                timeout=cfg.timeout_sec,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            return OcrResult(error=f'OCR 调用异常：{exc}')

    def _apply_result(self, record: ImageRecord, result: OcrResult | None,
                      suggester: NameSuggester) -> None:
        """把识别结果写回记录并生成命名建议（只在单线程里调用）。"""
        record.ocr_error = None

        if result is None:
            result = OcrResult(error='OCR 未返回结果')

        if result.canceled:
            record.status = Status.CANCELED
            record.error = '已强制停止'
            record.ocr_text = ''
            record.extracted_name = ''
            self._log_ocr(record)
            return

        record.ocr_engine = result.engine
        record.ocr_confidence = result.avg_confidence
        record.ocr_text = result.text or ''
        record.ocr_error = result.error or None
        record.ocr_boxes = list(result.boxes or [])
        record.ocr_image_size = tuple(result.image_size or (0, 0))

        if not record.ocr_text.strip():
            record.extracted_name = ''
            record.extract_method = ''
            record.extract_reason = ''
            record.status = Status.NEEDS_REVIEW
            record.error = record.ocr_error or '未识别到任何文字'
            self._log_ocr(record)
            return

        # 解析 + 命名（指定字段 → AI → 人工）
        parse_result = parse_ocr_text(record.ocr_text, self.config.naming.fields,
                                      result.scores)
        record.ocr_lines = [line.text for line in parse_result.lines]
        suggestion = suggester.suggest(record.ocr_text,
                                       field_hits=parse_result.fields,
                                       confidences=result.scores)
        suggestion = self._apply_correction_memory(record, suggestion)
        self._apply_suggestion(record, suggestion)
        self._log_ocr(record)

    def _apply_correction_memory(self, record: ImageRecord, suggestion):
        """历史修正记忆：只对"完全相同的 OCR 文本"自动生效。

        刻意不做模糊匹配 —— 同一套表单换个姓名时，两段 OCR 文本相似度
        高达 99%，模糊匹配会把上一个人的名字安到这个人头上。
        人工修正只该在"确实是同一份内容"时复用。
        """
        memory = self.correction_memory
        if memory is None or not record.ocr_text:
            return suggestion

        field = (self.config.naming.fields or [''])[0]
        hit = memory.lookup(record.ocr_text, field)
        if not hit or not hit.ok:
            return suggestion

        from ocr.suggester import Suggestion
        return Suggestion(
            name=hit.name,
            confidence=1.0,
            method=ExtractMethod.LEARNED,
            reason=(f'历史修正记忆：相同文本此前已人工修正为「{hit.name}」'
                    f'（{hit.use_count} 次）'),
            reliable=True,
        )

    def _apply_suggestion(self, record: ImageRecord, suggestion) -> None:
        """把建议写回记录并决定状态。"""
        name = (suggestion.name or '').strip()
        record.extracted_name = name
        record.ocr_predicted_name = name      # 系统原始建议（人工修改前的值）
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

    def set_manual_name(self, record: ImageRecord, name: str,
                        remember: bool = True) -> None:
        """人工指定名称（采用）。

        如果这次输入与系统建议不同，就是一次"修正"——记入纠正记忆，
        下次遇到完全相同的 OCR 文本会自动沿用。
        """
        name = (name or '').strip()
        previous = (record.extracted_name or '').strip()

        record.extracted_name = name
        record.extract_method = ExtractMethod.MANUAL
        record.extract_reason = '人工确认/修改'
        record.manual_override = True
        record.status = Status.READY if record.extracted_name else Status.NEEDS_REVIEW
        if record.status == Status.READY:
            record.error = None

        if remember and name and name != previous and record.ocr_text:
            self.remember_correction(record, previous, name)

    def remember_correction(self, record: ImageRecord, suggested: str,
                            corrected: str) -> bool:
        """把一次人工修正写入记忆库。"""
        if self.correction_memory is None:
            return False
        field = (self.config.naming.fields or [''])[0]
        saved = self.correction_memory.remember(
            ocr_text=record.ocr_text,
            field=field,
            suggested=suggested,
            corrected=corrected,
            source_folder=record.source_folder,
        )
        if saved and self.log:
            self.log.info(f'已记录修正："{suggested or "(无)"}" -> "{corrected}"'
                          f'（字段 {field or "未指定"}）')
        return saved

    def correction_stats(self) -> dict:
        if self.correction_memory is None:
            return {'count': 0, 'fields': {}}
        return self.correction_memory.stats()

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

    def set_output_dir(self, path: str) -> tuple[bool, str]:
        """OCR 之后修改输出目录：只重算目标路径，不用重新识别。

        用户常见失误：一开始把输出目录选成了源目录里的某个子文件夹。
        OCR 跑完才发现，若是只能重来一遍就很气人。这里只改配置 + 重算
        final_filename / dest_path，识别结果全部保留。
        """
        path = (path or '').strip().strip('"')
        if not path:
            return False, '输出目录不能为空'
        try:
            path = os.path.abspath(os.path.expanduser(path))
        except Exception as exc:
            return False, f'路径无效：{exc}'
        root = (self.config.rule.root_dir or '').strip()
        if root and os.path.normcase(os.path.abspath(root)) == os.path.normcase(path):
            return False, '输出目录不能与扫描目录相同（源文件只读）'
        if os.path.exists(path) and not os.path.isdir(path):
            return False, '该路径已存在且不是文件夹'
        try:
            file_utils.ensure_dir(path)
        except Exception as exc:
            return False, f'无法创建输出目录：{exc}'

        self.config.output.output_dir = path
        try:
            self.rebuild_names(only_selected=True)
        except TaskError as exc:
            return False, str(exc)
        if self.log:
            self.log.info(f'输出目录已改为 {path}（文件名已重新计算，无需重新 OCR）')
        return True, ''

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

    def export_training_data(self, out_dir: str) -> dict:
        """导出本次任务的 OCR 训练数据（单行截图 + 标签）。"""
        from reports.training_export import export_training_data

        stats = export_training_data(self.records, out_dir,
                                     self.task_id or 'current')
        if self.log:
            self.log.info(f'已导出训练数据：{stats["exported"]} 条 -> {stats["root"]}')
            if stats['no_box'] or stats['no_line']:
                self.log.warn(f'未能导出：缺文本框 {stats["no_box"]} 条，'
                              f'未定位到行 {stats["no_line"]} 条')
        return stats

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

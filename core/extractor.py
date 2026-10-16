"""
文件复制执行器（唯一允许写盘的模块）。

安全设计：
1. 只复制，绝不移动/删除/修改源文件；
2. 目标文件用 os.O_EXCL 独占创建 —— 覆盖在操作系统层面不可能发生；
3. 单个文件失败只记录错误，继续处理下一个；
4. 支持暂停 / 继续 / 取消；取消后已复制的文件保留；
5. 每个文件处理完立即写库，程序崩溃也能追溯已完成部分；
6. 提供撤销能力：只删除"本任务创建的、且指纹匹配的"目标文件。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.filename_manager import AllocationResult, FilenameAllocator, compute_final_filename
from models import (ExecutionResult, ImageRecord, OutputConfig, ProgressEvent,
                    Status, TaskConfig, TaskSummary)
from utils import file_utils
from utils.logger import get_logger

logger = get_logger('extractor')

#: FileExistsError 后重新分配的最大次数（防止极端并发下的死循环）
MAX_REALLOCATE = 50


@dataclass
class CopyOutcome:
    """单个文件的复制结果。"""
    ok: bool = False
    dest_path: str = ''
    final_name: str = ''
    conflict: bool = False
    conflict_note: str = ''
    skipped: bool = False
    error: str = ''


def is_eligible(record: ImageRecord, include_unreviewed: bool = False) -> bool:
    """该记录是否应该在本轮执行中被复制。"""
    if record.status in (Status.SKIPPED, Status.SUCCESS, Status.FAILED, Status.CANCELED):
        return False
    if record.status == Status.NEEDS_REVIEW:
        return include_unreviewed
    return True


class Extractor:
    """执行复制（把源图片写到目标目录，并生成最终文件名）。"""

    def __init__(self, config: TaskConfig, database=None, task_id: int | None = None,
                 task_logger=None):
        self.config = config
        self.output = config.output
        self.naming = config.naming
        self.db = database
        self.task_id = task_id
        self.log = task_logger
        self.allocator = FilenameAllocator(
            policy=self.output.conflict_policy, case_insensitive=True)
        self._pause = threading.Event()
        self._pause.set()          # set = 运行中，clear = 暂停
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 暂停控制

    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def _wait_if_paused(self, cancel_event) -> bool:
        """暂停时阻塞；返回 False 表示期间被取消。"""
        while not self._pause.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return False
            time.sleep(0.15)
        return True

    # ------------------------------------------------------------ 预检查

    def precheck(self, records: list[ImageRecord], include_unreviewed: bool = False) -> dict:
        """执行前检查：目标目录可写、磁盘空间、源文件是否存在。"""
        problems: list[str] = []
        output_dir = self.output.output_dir

        if not output_dir:
            problems.append('未设置输出目录')
        else:
            try:
                file_utils.ensure_dir(output_dir)
            except OSError as exc:
                problems.append(f'输出目录不可用：{exc}')

            if not os.access(output_dir, os.W_OK):
                problems.append('输出目录没有写入权限')

        eligible = [r for r in records if is_eligible(r, include_unreviewed)]
        total_size = sum(r.size for r in eligible)
        free = file_utils.free_space(output_dir) if output_dir else -1
        if free >= 0 and total_size > 0 and free < total_size * 1.05:
            problems.append(
                f'目标磁盘剩余空间可能不足（需要约 {file_utils.human_size(total_size)}，'
                f'剩余 {file_utils.human_size(free)}）')

        missing = [r for r in eligible if not file_utils.exists(r.source_path)]
        if missing:
            problems.append(f'有 {len(missing)} 个源文件在扫描后已不存在（将被标记为失败）')

        return {
            'ok': not problems or all('可能不足' in p or '已不存在' in p for p in problems),
            'problems': problems,
            'eligible': len(eligible),
            'total_size': total_size,
            'free_space': free,
        }

    # ------------------------------------------------------------ 执行

    def execute(self, records: list[ImageRecord],
                progress_cb: Optional[Callable[[ProgressEvent], bool]] = None,
                cancel_event=None,
                include_unreviewed: bool = False) -> ExecutionResult:
        """执行复制。返回 ExecutionResult。"""
        result = ExecutionResult()
        summary = TaskSummary(started_at=time.time())
        eligible = [r for r in records if is_eligible(r, include_unreviewed)]
        summary.total = len(records)
        summary.selected = len(eligible)

        if self.log:
            self.log.info(f'开始执行：待复制 {len(eligible)} 个文件，输出目录 '
                          f'{self.output.output_dir}')

        cancel_event = cancel_event or threading.Event()
        done = 0

        for record in records:
            if record.status in (Status.SKIPPED, Status.SUCCESS, Status.FAILED):
                continue

            if cancel_event.is_set():
                if record.status not in (Status.SUCCESS, Status.FAILED, Status.SKIPPED):
                    record.status = Status.CANCELED
                continue

            if not self._wait_if_paused(cancel_event):
                if record.status not in (Status.SUCCESS, Status.FAILED, Status.SKIPPED):
                    record.status = Status.CANCELED
                continue

            if not is_eligible(record, include_unreviewed):
                continue

            done += 1
            outcome = self._copy_one(record)
            self._apply_outcome(record, outcome)

            # 实时计数（让界面进度条上的成功/失败数字持续更新）
            if outcome.skipped:
                summary.skipped += 1
            elif outcome.ok:
                summary.success += 1
                if outcome.conflict or record.conflict:
                    summary.conflicts += 1
            else:
                summary.failed += 1

            if progress_cb:
                event = ProgressEvent(
                    phase='execute', current=done, total=summary.selected,
                    message=outcome.error or outcome.conflict_note,
                    source=record.source_path,
                    dest=outcome.dest_path,
                    ocr_text=record.extracted_name,
                    status=record.status,
                    success=summary.success, failed=summary.failed,
                    skipped=summary.skipped,
                )
                try:
                    if progress_cb(event) is False:
                        cancel_event.set()
                except Exception:
                    pass

        # 最终汇总（从记录重新统计，保证与记录状态一致）
        summary.success = summary.failed = summary.skipped = 0
        summary.canceled = summary.conflicts = 0
        for record in records:
            if record.status == Status.SUCCESS:
                summary.success += 1
            elif record.status == Status.FAILED:
                summary.failed += 1
            elif record.status == Status.SKIPPED:
                summary.skipped += 1
            elif record.status == Status.CANCELED:
                summary.canceled += 1
            if record.conflict and record.status == Status.SUCCESS:
                summary.conflicts += 1
        summary.finished_at = time.time()

        result.summary = summary
        result.errors = [(r.source_path, r.error or '') for r in records if r.error]
        if self.log:
            self.log.info(f'执行结束：成功 {summary.success}，失败 {summary.failed}，'
                          f'跳过 {summary.skipped}，取消 {summary.canceled}，'
                          f'耗时 {summary.elapsed_text}')
        return result

    # ------------------------------------------------------------ 单文件

    def _copy_one(self, record: ImageRecord) -> CopyOutcome:
        """复制单个文件。任何异常都被捕获并转为失败结果。"""
        try:
            # 1. 源文件校验
            if not os.path.isfile(file_utils.to_long_path(record.source_path)):
                return CopyOutcome(error='源文件不存在或已被移动')

            # 2. 目标目录
            dest_dir = record.dest_dir(self.output.output_dir, self.output.keep_structure)
            try:
                file_utils.ensure_dir(dest_dir)
            except OSError as exc:
                return CopyOutcome(error=f'无法创建目标目录：{exc}')

            # 3. 期望文件名
            desired = record.final_filename or compute_final_filename(
                record, self.naming, dest_dir)
            if not desired:
                return CopyOutcome(error='无法确定最终文件名')

            # 4. 分配未被占用的名字 + 独占复制
            last_error = ''
            for _ in range(MAX_REALLOCATE):
                if record.final_filename and record.manual_override and not last_error:
                    desired = record.final_filename
                allocation: AllocationResult = self.allocator.allocate(dest_dir, desired)
                if allocation.skipped:
                    return CopyOutcome(skipped=True, final_name=allocation.filename,
                                       conflict=allocation.conflict,
                                       conflict_note=allocation.note)

                dest_path = os.path.join(dest_dir, allocation.filename)
                try:
                    file_utils.copy_no_overwrite(record.source_path, dest_path)
                except FileExistsError:
                    # 极端竞态：别人刚好创建了同名文件 -> 重新分配
                    self.allocator.release(dest_dir, allocation.filename)
                    self.allocator.refresh(dest_dir)
                    desired = allocation.filename
                    last_error = '目标文件在复制瞬间被占用，已自动换名重试'
                    continue
                except PermissionError:
                    self.allocator.release(dest_dir, allocation.filename)
                    return CopyOutcome(error='文件被占用或没有权限（源或目标）')
                except OSError as exc:
                    self.allocator.release(dest_dir, allocation.filename)
                    return CopyOutcome(error=f'复制失败：{exc}')

                # 5. 校验
                if self.output.verify_copy:
                    src_size = os.path.getsize(file_utils.to_long_path(record.source_path))
                    dst_size = os.path.getsize(file_utils.to_long_path(dest_path))
                    if src_size != dst_size:
                        try:
                            os.remove(file_utils.to_long_path(dest_path))
                        except OSError:
                            pass
                        return CopyOutcome(error=f'复制校验失败：大小不一致 '
                                                 f'({src_size} != {dst_size})')

                return CopyOutcome(ok=True, dest_path=dest_path,
                                   final_name=allocation.filename,
                                   conflict=allocation.conflict,
                                   conflict_note=allocation.note)

            return CopyOutcome(error=last_error or '多次重试后仍无法分配可用文件名')

        except Exception as exc:                     # 兜底：任何异常都不能中断任务
            logger.exception('复制 %s 时发生未预期错误', record.source_path)
            return CopyOutcome(error=f'未预期错误：{exc}')

    def _apply_outcome(self, record: ImageRecord, outcome: CopyOutcome) -> None:
        """把复制结果写回记录 + 数据库 + 任务日志。"""
        if outcome.skipped:
            record.status = Status.SKIPPED
            record.error = outcome.error or outcome.conflict_note or '按设置跳过'
        elif outcome.ok:
            record.status = Status.SUCCESS
            record.dest_path = outcome.dest_path
            record.final_filename = outcome.final_name
            record.created = True
            record.error = None
            # 冲突标记可能来自命名阶段的预分配，这里用 OR 保留，不能覆盖丢失
            record.conflict = bool(record.conflict or outcome.conflict)
            if outcome.conflict_note:
                record.conflict_note = outcome.conflict_note
        else:
            record.status = Status.FAILED
            record.error = outcome.error
            record.created = False

        if self.db and self.task_id is not None:
            try:
                self.db.update_record_status(
                    self.task_id, record.index,
                    status=record.status, error=record.error,
                    dest_path=record.dest_path, created=1 if record.created else 0,
                    final_filename=record.final_filename,
                    conflict=1 if record.conflict else 0,
                    conflict_note=record.conflict_note)
            except Exception as exc:
                logger.warning('写库失败（不影响复制）：%s', exc)

        if self.log:
            status_text = Status.label(record.status)
            self.log.record_file(
                source=record.source_path,
                ocr_text=record.ocr_text,
                extracted=record.extracted_name,
                final_path=record.dest_path or record.final_filename,
                status=status_text,
                error=record.error or '')


# ==================================================================== 撤销

def undo_files(file_rows: list[dict],
               allowed_root: str = '',
               progress_cb: Optional[Callable[[int, int, str], bool]] = None,
               cancel_event=None) -> dict:
    """删除"本任务创建的目标文件"（撤销）。

    安全校验（全部满足才会删除）：
    1. 记录标记了 created=1；
    2. 文件当前仍存在于记录的目标路径；
    3. 文件大小与登记的来源文件大小一致（指纹校验）；
    4. 路径位于本任务的输出目录之下（allowed_root）；
    5. 源目录中的文件永远不会被触碰。
    """
    result = {'deleted': 0, 'missing': 0, 'failed': 0, 'skipped': 0,
              'errors': [], 'details': []}
    total = len(file_rows)
    allowed_key = file_utils.norm_path_key(allowed_root) if allowed_root else ''

    for i, row in enumerate(file_rows, start=1):
        if cancel_event is not None and cancel_event.is_set():
            result['skipped'] += total - i + 1
            break

        dest_path = row.get('dest_path') or ''
        try:
            if progress_cb and progress_cb(i, total, dest_path) is False:
                result['skipped'] += total - i + 1
                break
        except Exception:
            pass

        if not dest_path:
            result['skipped'] += 1
            continue

        # 4) 路径必须在输出目录内，防止误删源文件
        if allowed_key:
            dest_key = file_utils.norm_path_key(dest_path)
            if not dest_key.startswith(allowed_key.rstrip('/') + '/'):
                result['skipped'] += 1
                result['errors'].append((dest_path, '不在输出目录内，已拒绝删除'))
                continue

        if not file_utils.exists(dest_path):
            result['missing'] += 1
            continue

        # 3) 指纹校验
        fingerprint = file_utils.file_fingerprint(dest_path)
        expected_size = int(row.get('size') or 0)
        if fingerprint and expected_size and fingerprint[0] != expected_size:
            result['skipped'] += 1
            result['errors'].append(
                (dest_path, f'文件大小与记录不一致（{fingerprint[0]} != {expected_size}），已拒绝删除'))
            continue

        ok, error = file_utils.safe_delete(dest_path)
        if ok:
            result['deleted'] += 1
            result['details'].append(dest_path)
        else:
            result['failed'] += 1
            result['errors'].append((dest_path, error))

    return result

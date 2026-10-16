"""
后台工作线程（QThread）。

所有耗时操作（扫描 / OCR / 复制 / 撤销）都在这里执行，
界面线程永远只处理信号与绘制，保证大任务时 GUI 不卡死。

- progress  : 进度事件（ProgressEvent 或 (current, total, message)）
- log       : 日志文本（转发给界面日志框）
- done      : 完成，携带结果对象
- failed    : 失败，携带错误信息
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from models import ProgressEvent
from utils.logger import get_logger

logger = get_logger('worker')


class BaseWorker(QThread):
    """工作线程基类：统一处理取消 / 暂停 / 异常。"""

    progress = Signal(object)
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()          # set = 运行中
        self._log_buffer: list[str] = []

    # -------------------------------------------------- 控制

    def cancel(self) -> None:
        """请求取消（并解除暂停，否则线程会一直停在暂停点）。"""
        self.cancel_event.set()
        self.pause_event.set()

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    @property
    def paused(self) -> bool:
        return not self.pause_event.is_set()

    @property
    def canceled(self) -> bool:
        return self.cancel_event.is_set()

    # -------------------------------------------------- 运行

    def run(self) -> None:                      # noqa: D102
        try:
            result = self.do_work()
        except Exception as exc:                # 任何异常都不允许逃逸
            logger.exception('后台任务异常')
            try:
                self.failed.emit(str(exc))
            except RuntimeError:
                pass
            return
        try:
            self.done.emit(result)
        except RuntimeError:
            pass

    def do_work(self):
        """子类实现。"""
        raise NotImplementedError

    def emit_log(self, text: str) -> None:
        """线程安全地发日志。"""
        try:
            self.log.emit(text)
        except RuntimeError:
            pass


class ScanWorker(BaseWorker):
    """扫描线程（只读，不会改动任何文件）。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager

    def do_work(self):
        def on_progress(scanned_dirs: int, found: int, current: str):
            self.progress.emit(ProgressEvent(
                phase='scan', current=scanned_dirs, total=0,
                message=current, extra={'found': found}))
            return not self.cancel_event.is_set()

        return self.manager.scan(progress_cb=on_progress,
                                 cancel_event=self.cancel_event)


class OcrWorker(BaseWorker):
    """OCR 与命名线程（只读源图片）。"""

    def __init__(self, manager, only: str = '', parent=None):
        super().__init__(parent)
        self.manager = manager
        self.only = only

    def do_work(self):
        def on_progress(event: ProgressEvent):
            self.progress.emit(event)
            return not self.cancel_event.is_set()

        return self.manager.run_ocr(progress_cb=on_progress,
                                    cancel_event=self.cancel_event,
                                    pause_event=self.pause_event,
                                    only=self.only)


class SingleOcrWorker(BaseWorker):
    """单张重新 OCR。"""

    def __init__(self, manager, record, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.record = record

    def do_work(self):
        self.manager.re_ocr_one(self.record)
        return self.record


class ExecuteWorker(BaseWorker):
    """复制执行线程（唯一会写盘的线程）。"""

    def __init__(self, manager, include_unreviewed: bool = False, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.include_unreviewed = include_unreviewed

    def do_work(self):
        def on_progress(event: ProgressEvent):
            self.progress.emit(event)
            return not self.cancel_event.is_set()

        return self.manager.execute(progress_cb=on_progress,
                                    cancel_event=self.cancel_event,
                                    include_unreviewed=self.include_unreviewed)


class UndoWorker(BaseWorker):
    """撤销线程（只删除本任务创建的目标文件）。"""

    def __init__(self, manager, task_id=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.task_id = task_id

    def do_work(self):
        def on_progress(current: int, total: int, path: str):
            self.progress.emit(ProgressEvent(
                phase='undo', current=current, total=total, message=path))
            return not self.cancel_event.is_set()

        return self.manager.undo(task_id=self.task_id, progress_cb=on_progress,
                                 cancel_event=self.cancel_event)

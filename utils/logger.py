"""
日志系统。

- 应用级日志：%LOCALAPPDATA%/PicHarvest/logs/app.log（滚动，5MB × 3）
- 任务级日志：%LOCALAPPDATA%/PicHarvest/logs/task_<id>_<时间>.log
- TaskLogger 同时支持回调，便于把日志实时推送到 GUI 文本框
- 日志只写本地磁盘，绝不上传
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from typing import Callable, Optional

from utils import file_utils

_APP_LOGGER_NAME = 'picharvest'
_configured = False
_lock = threading.Lock()


def setup_app_logging(level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """初始化应用级日志（只执行一次）。"""
    global _configured
    with _lock:
        logger = logging.getLogger(_APP_LOGGER_NAME)
        if _configured:
            return logger
        logger.setLevel(level)
        logger.propagate = False

        fmt = logging.Formatter(
            '[%(asctime)s] %(levelname)-7s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )

        try:
            log_path = os.path.join(file_utils.get_log_dir(), 'app.log')
            handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        except Exception:
            pass

        if console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(fmt)
            logger.addHandler(console_handler)

        _configured = True
        return logger


def get_logger(name: str = '') -> logging.Logger:
    """获取子 logger。"""
    base = setup_app_logging()
    return base.getChild(name) if name else base


def timestamp(fmt: str = '%Y-%m-%d %H:%M:%S') -> str:
    return time.strftime(fmt)


def file_timestamp() -> str:
    return time.strftime('%Y%m%d_%H%M%S')


class TaskLogger:
    """单个任务的日志：写文件 + 可选回调（用于 GUI 实时显示）。"""

    def __init__(self, task_id: int | str, echo: Optional[Callable[[str], None]] = None,
                 log_dir: str | None = None):
        self.task_id = task_id
        self.echo = echo
        self._lock = threading.Lock()
        self._closed = False
        log_dir = log_dir or file_utils.get_log_dir()
        self.path = os.path.join(log_dir, f'task_{task_id}_{file_timestamp()}.log')
        self._file = None
        try:
            file_utils.ensure_dir(log_dir)
            self._file = open(self.path, 'a', encoding='utf-8')
            self.write('INFO', f'任务 {task_id} 日志开始')
        except Exception:
            self._file = None

    # -------------------------------------------------- 基础写入

    def write(self, level: str, message: str) -> None:
        line = f'[{timestamp()}] {level:<5} {message}'
        with self._lock:
            if self._file and not self._closed:
                try:
                    self._file.write(line + '\n')
                    self._file.flush()
                except Exception:
                    pass
        if self.echo:
            try:
                self.echo(line)
            except Exception:
                pass

    def info(self, message: str) -> None:
        self.write('INFO', message)

    def warn(self, message: str) -> None:
        self.write('WARN', message)

    def error(self, message: str) -> None:
        self.write('ERROR', message)

    # -------------------------------------------------- 结构化记录

    def record_file(self, source: str, ocr_text: str, extracted: str,
                    final_path: str, status: str, error: str = '') -> None:
        """按需求书格式记录单文件处理结果。"""
        self.write('INFO', '-' * 60)
        self.write('INFO', f'来源：{source}')
        ocr_brief = (ocr_text or '').replace('\n', ' | ')[:500]
        self.write('INFO', f'OCR：{ocr_brief or "(空)"}')
        self.write('INFO', f'识别名称：{extracted or "(空)"}')
        self.write('INFO', f'最终文件：{final_path or "(无)"}')
        self.write('INFO', f'状态：{status}')
        if error:
            self.write('ERROR', f'错误：{error}')

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._file:
                try:
                    self._file.write(f'[{timestamp()}] INFO  任务 {self.task_id} 日志结束\n')
                    self._file.close()
                except Exception:
                    pass
                self._file = None

"""
子进程 OCR 引擎（父进程侧）。

能力：
- **强制停止**：kill 子进程，正在跑的推理立刻结束（线程模式做不到）
- **单张超时**：到点直接杀进程，不会永远卡住
- 模型只加载一次（子进程常驻，整批任务复用）

设计要点：
- 生命周期锁 `_lifecycle_lock` 只保护"启动/杀死"这类短操作，
  `recognize_path` 等待结果时不持锁，否则强制停止会被阻塞。
- 每次启动使用新的响应队列，避免被上一个进程的残留消息污染。
- stderr 由独立线程持续读走，防止管道写满导致子进程阻塞。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque

from ocr.engine import OcrEngine, OcrResult
from utils.logger import get_logger

logger = get_logger('ocr.process')

#: 等待结果的轮询间隔（决定强制停止的响应速度）
POLL_INTERVAL = 0.15
#: 子进程启动后等待 ready 的最大时间兜底
DEFAULT_STARTUP_TIMEOUT = 90


class ProcessOcrEngine(OcrEngine):
    """把 OCR 推理放到独立进程里执行。"""

    name = 'process'
    display_name = 'OCR 子进程'

    def __init__(self, prefer: str = 'auto', rec_model_path: str = '',
                 det_model_path: str = '',
                 startup_timeout: int = DEFAULT_STARTUP_TIMEOUT):
        self.prefer = prefer or 'auto'
        self.rec_model_path = rec_model_path or ''
        self.det_model_path = det_model_path or ''
        self.startup_timeout = max(10, int(startup_timeout or DEFAULT_STARTUP_TIMEOUT))

        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue | None = None
        self._lifecycle_lock = threading.RLock()
        self._stderr_tail: deque = deque(maxlen=30)
        self._engine_label = ''
        self._last_error = ''
        self._ready = False
        self._stopped = False        # 是否被 force_stop 主动终止

        atexit.register(self.release)

    # ================================================================ 生命周期

    def _worker_command(self) -> list[str]:
        """构造子进程启动命令（兼容源码运行与 PyInstaller 打包）。"""
        if getattr(sys, 'frozen', False):
            # 打包后：用同一个 exe 加参数启动子进程
            return [sys.executable, '--ocr-worker']
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [sys.executable, os.path.join(root, 'main.py'), '--ocr-worker']

    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def ready(self) -> bool:
        """子进程是否已完成模型加载、可以接受请求。"""
        return self._ready and self.is_alive()

    def start(self) -> tuple[bool, str]:
        """启动子进程并等待模型就绪。返回 (成功, 错误信息)。

        注意：等待就绪的过程**不持锁** —— 否则用户在模型加载期间点
        "强制停止"会一直阻塞在锁上，反而停不下来（这正是要修的问题）。
        """
        with self._lifecycle_lock:
            if self.is_alive() and self._ready:
                return True, ''
            self._ready = False
            self._stopped = False
            self._kill_locked()

            popen_kwargs = dict(
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
            if os.name == 'nt':
                # 不弹黑色控制台窗口（打包成 GUI 程序时尤其重要）
                popen_kwargs['creationflags'] = getattr(
                    subprocess, 'CREATE_NO_WINDOW', 0x08000000)

            try:
                proc = subprocess.Popen(self._worker_command(), **popen_kwargs)
            except Exception as exc:
                self._last_error = f'无法启动 OCR 子进程：{exc}'
                logger.error(self._last_error)
                return False, self._last_error

            responses: queue.Queue = queue.Queue()
            self._proc = proc
            self._queue = responses
            self._stderr_tail.clear()

            threading.Thread(target=self._read_stdout, args=(proc, responses),
                             daemon=True, name='ocr-stdout').start()
            threading.Thread(target=self._read_stderr, args=(proc,),
                             daemon=True, name='ocr-stderr').start()

            # 发送配置
            try:
                request = {
                    'engine': self.prefer,
                    'rec_model_path': self.rec_model_path,
                    'det_model_path': self.det_model_path,
                }
                proc.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._kill_locked()
                self._last_error = f'无法向 OCR 子进程发送配置：{exc}'
                return False, self._last_error

        # ---- 以下等待就绪的过程不持锁，保证"强制停止"随时可执行 ----
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self._stopped:
                return False, '启动已被强制停止'
            if self._proc is not proc:
                return False, '启动已被强制停止'
            try:
                line = responses.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get('type') == 'ready':
                if data.get('ok'):
                    self._engine_label = data.get('engine', '')
                    self._ready = True
                    logger.info('OCR 子进程就绪：%s', self._engine_label)
                    return True, ''
                self._kill_locked()
                self._ready = False
                self._last_error = (data.get('error')
                                    or 'OCR 子进程初始化失败')
                logger.error('%s', self._last_error)
                return False, self._last_error

        detail = '\n'.join(self._stderr_tail)[-600:]
        self._kill_locked()
        self._ready = False
        if self._stopped:
            return False, '启动已被强制停止'
        self._last_error = (f'OCR 子进程启动超时（{self.startup_timeout} 秒）'
                            + (f'\n{detail}' if detail else ''))
        logger.error('%s', self._last_error)
        return False, self._last_error

    # ================================================================ 识别

    def recognize_path(self, path: str, preset: str = 'standard',
                       max_side: int = 2200, timeout: float | None = None,
                       cancel_event=None) -> OcrResult:
        """把图片路径发给子进程识别，支持随时取消与超时。"""
        if cancel_event is not None and cancel_event.is_set():
            return OcrResult(engine=self.name, error='已取消', canceled=True)

        ok, error = self.start()
        if not ok:
            return OcrResult(engine=self.name, error=error)

        with self._lifecycle_lock:
            proc = self._proc
            responses = self._queue
            engine_label = self._engine_label

        if proc is None or responses is None:
            return OcrResult(engine=self.name, error='OCR 子进程未就绪')

        try:
            request = {'path': path, 'preset': preset, 'max_side': int(max_side)}
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self.force_stop()
            return OcrResult(engine=self.name,
                             error=f'OCR 子进程已退出：{exc}')

        limit = float(timeout) if timeout and timeout > 0 else 0.0
        deadline = time.time() + limit if limit else 0.0

        while True:
            # 用户点了"强制停止" / 取消
            if cancel_event is not None and cancel_event.is_set():
                self.force_stop()
                return OcrResult(engine=self.name, error='已强制停止',
                                 canceled=True)

            if deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.force_stop()
                    return OcrResult(
                        engine=self.name,
                        error=f'单张识别超时（{limit:.0f} 秒），已强制终止')
            else:
                remaining = POLL_INTERVAL

            try:
                line = responses.get(timeout=min(POLL_INTERVAL, max(0.01, remaining)))
            except queue.Empty:
                continue

            if line is None:
                # 子进程退出（崩溃 / 被强制停止）
                detail = '\n'.join(self._stderr_tail)[-500:]
                stopped = self._stopped
                self.force_stop()
                if stopped:
                    return OcrResult(engine=self.name, canceled=True,
                                     error='已强制停止')
                return OcrResult(
                    engine=self.name,
                    error='OCR 子进程异常退出' + (f'：{detail}' if detail else ''))

            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get('type') == 'ready':
                continue

            if data.get('ok') is False and not data.get('text'):
                return OcrResult(engine=data.get('engine') or engine_label,
                                 error=data.get('error') or '识别失败')

            size = data.get('size') or [0, 0]
            return OcrResult(
                text=data.get('text', ''),
                lines=list(data.get('lines') or []),
                scores=[float(s) for s in (data.get('scores') or [])],
                boxes=list(data.get('boxes') or []),
                engine=data.get('engine') or engine_label,
                elapsed=float(data.get('elapsed') or 0.0),
                error=data.get('error') or '',
                image_size=(int(size[0]), int(size[1])) if len(size) >= 2 else (0, 0),
            )

    def recognize(self, image) -> OcrResult:
        """接口兼容：子进程模式只接受路径。"""
        if isinstance(image, str):
            return self.recognize_path(image)
        return OcrResult(engine=self.name,
                         error='子进程模式只支持按路径识别，请先保存图片')

    # ================================================================ 停止 / 释放

    def force_stop(self) -> bool:
        """强制终止子进程（立即生效）。返回是否确实杀掉了进程。"""
        self._stopped = True
        with self._lifecycle_lock:
            alive = self.is_alive()
            self._kill_locked()
            return alive

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()               # Windows: TerminateProcess，立即生效
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def release(self) -> None:
        with self._lifecycle_lock:
            self._kill_locked()

    def load(self) -> None:
        ok, error = self.start()
        if not ok:
            raise RuntimeError(error)

    def warmup(self) -> bool:
        ok, _error = self.start()
        return ok

    # ================================================================ 管道读取

    def _read_stdout(self, proc, responses: queue.Queue) -> None:
        """持续读取子进程 stdout，把每一行放进队列；EOF 时放哨兵。"""
        try:
            for line in proc.stdout:
                responses.put(line)
        except (ValueError, OSError):
            pass
        finally:
            responses.put(None)

    def _read_stderr(self, proc) -> None:
        """读走 stderr，保留最后若干行用于报错诊断。"""
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line)
        except (ValueError, OSError):
            pass

    # ================================================================

    @property
    def engine_label(self) -> str:
        return self._engine_label or self.display_name

    @property
    def last_error(self) -> str:
        return self._last_error

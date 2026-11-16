"""
OCR 子进程池：多个子进程并发识别，把批量速度拉起来。

为什么是"多进程"而不是"多线程"：
- onnxruntime 的推理本身会吃满多个核，线程并发只会互相抢 GIL 与 CPU；
- 单张识别是纯 CPU 密集的，真正能线性加速的方式就是多个进程各自跑一核；
- 复用已有的 ProcessOcrEngine：每个子进程常驻、模型只加载一次，
  既能并发，又能保留"强制停止直接 kill"的能力。

**实测结论（重要，别被"多进程一定更快"骗了）**：
onnxruntime 默认会把一个进程的推理铺到所有逻辑核上。在本机（16 逻辑核）
16 张图的对比里，串行 10.4s，2 进程 11.9s，4 进程 18.0s——进程越多越慢，
因为每个进程都在抢同一批核，还要额外付进程唤醒与内存带宽的代价。
所以默认 `workers=1`（串行）；只有单进程明显吃不饱 CPU（核多、模型小、
图片小）时，调大才可能有收益。池本身照常用，谁空谁接。

代价：每个子进程各自占一份模型内存（RapidOCR 约 200–400MB）。

用法与单引擎完全一致（同一个 OcrEngine 接口），task_manager 无需区分。
"""

from __future__ import annotations

import atexit
import os
import queue
import threading

from ocr.engine import OcrEngine, OcrResult
from ocr.process_engine import DEFAULT_STARTUP_TIMEOUT, ProcessOcrEngine
from utils.logger import get_logger

logger = get_logger('ocr.pool')

#: 自动并发的上限（再多收益递减，内存翻倍）
MAX_AUTO_WORKERS = 4
#: 等空闲引擎时的轮询间隔（决定取消响应速度）
POLL_INTERVAL = 0.15


def auto_worker_count(cpu_count: int | None = None) -> int:
    """自动并发数：物理核的一半，至少 1，最多 4。

    用一半而不是全部：OCR 时界面线程、磁盘、其它程序都要用 CPU，
    抢满所有核反而让整机卡顿。
    """
    cpus = int(cpu_count or os.cpu_count() or 2)
    return max(1, min(MAX_AUTO_WORKERS, cpus // 2 or 1))


def resolve_workers(requested: int | None, cpu_count: int | None = None) -> int:
    """把配置里的 workers 解析成实际并发数（0/负数 = 自动）。"""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return auto_worker_count(cpu_count)
    return max(1, min(MAX_AUTO_WORKERS * 2, value))


class OcrWorkerPool(OcrEngine):
    """N 个常驻子进程 + 空闲队列：谁空谁接下一张。"""

    name = 'process'
    display_name = 'OCR 子进程池'

    def __init__(self, size: int = 0, prefer: str = 'auto',
                 rec_model_path: str = '', det_model_path: str = '',
                 startup_timeout: int = DEFAULT_STARTUP_TIMEOUT):
        self.requested = max(1, int(size or 1))
        self.prefer = prefer or 'auto'
        self.rec_model_path = rec_model_path or ''
        self.det_model_path = det_model_path or ''
        self.startup_timeout = max(10, int(startup_timeout or DEFAULT_STARTUP_TIMEOUT))

        self._engines: list[ProcessOcrEngine] = []
        self._idle: queue.Queue = queue.Queue()
        self._start_lock = threading.Lock()
        self._stopped = False
        self._size = 0
        self._engine_label = ''
        self._last_error = ''
        atexit.register(self.release)

    # ================================================================ 生命周期

    def _make_engine(self) -> ProcessOcrEngine:
        return ProcessOcrEngine(
            prefer=self.prefer,
            rec_model_path=self.rec_model_path,
            det_model_path=self.det_model_path,
            startup_timeout=self.startup_timeout,
        )

    def start(self) -> tuple[bool, str]:
        """并行启动所有子进程，等它们各自就绪。

        部分失败不算失败：用活下来的那几个继续干活（并记一条警告），
        全部失败才返回 False，由 task_manager 退回进程内模式。
        """
        with self._start_lock:
            if not self._engines:
                self._engines = [self._make_engine() for _ in range(self.requested)]

            if self.ready:
                self._size = len(self._engines)
                self._stopped = False
                return True, ''

            pending = [engine for engine in self._engines if not engine.is_alive()]
            if not pending:
                self._reset_idle(self._engines)
                self._size = len(self._engines)
                self._stopped = False
                return True, ''

            outcomes: dict[int, tuple[bool, str]] = {}

            def boot(engine: ProcessOcrEngine) -> None:
                try:
                    outcomes[id(engine)] = engine.start()
                except Exception as exc:            # 不允许线程里抛异常
                    outcomes[id(engine)] = (False, f'子进程启动异常：{exc}')

            threads = [threading.Thread(target=boot, args=(engine,), daemon=True,
                                        name=f'ocr-boot-{i + 1}')
                       for i, engine in enumerate(pending)]
            for thread in threads:
                thread.start()
            # 模型加载本来就慢，这里给足时间；超时的引擎会在下面被丢弃
            for thread in threads:
                thread.join(timeout=self.startup_timeout + 20)

            alive = [engine for engine in self._engines if engine.ready]
            dropped = [engine for engine in self._engines if engine not in alive]
            for engine in dropped:
                try:
                    engine.release()
                except Exception:
                    pass

            self._engines = alive
            self._size = len(alive)
            self._stopped = False
            self._reset_idle(alive)
            self._last_error = ''

            if not alive:
                errors = [error for ok, error in outcomes.values() if not ok and error]
                for engine in dropped:
                    if engine.last_error:
                        errors.append(engine.last_error)
                self._last_error = errors[0] if errors else 'OCR 子进程池启动失败'
                return False, self._last_error

            if dropped:
                logger.warning('有 %d 个 OCR 子进程启动失败，改用剩余 %d 个并发',
                               len(dropped), len(alive))
            labels = {engine.engine_label for engine in alive}
            self._engine_label = labels.pop() if len(labels) == 1 else '多种引擎'
            logger.info('OCR 子进程池就绪：%d 个并发，引擎 %s',
                        self._size, self._engine_label)
            return True, ''

    def _reset_idle(self, engines: list[ProcessOcrEngine]) -> None:
        """重建空闲队列（启动/重启后调用，丢弃旧引用）。"""
        idle: queue.Queue = queue.Queue()
        for engine in engines:
            idle.put(engine)
        self._idle = idle

    def is_alive(self) -> bool:
        return any(engine.is_alive() for engine in self._engines)

    @property
    def ready(self) -> bool:
        return bool(self._engines) and any(engine.ready for engine in self._engines)

    @property
    def size(self) -> int:
        """实际可用的并发数（未启动时返回配置值）。"""
        return self._size or self.requested

    @property
    def engine_label(self) -> str:
        return self._engine_label or self.display_name

    @property
    def last_error(self) -> str:
        return self._last_error

    # ================================================================ 识别

    def _acquire(self, cancel_event):
        """等一个空闲子进程；取消/停止时返回 None。"""
        while True:
            if self._stopped:
                return None
            if cancel_event is not None and cancel_event.is_set():
                return None
            try:
                return self._idle.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                continue

    def recognize_path(self, path: str, preset: str = 'standard',
                       max_side: int = 2200, timeout: float | None = None,
                       cancel_event=None) -> OcrResult:
        if self._stopped:
            return OcrResult(engine=self.name, error='已强制停止', canceled=True)
        if cancel_event is not None and cancel_event.is_set():
            return OcrResult(engine=self.name, error='已取消', canceled=True)

        ok, error = self.start()
        if not ok:
            return OcrResult(engine=self.name, error=error)

        engine = self._acquire(cancel_event)
        if engine is None:
            return OcrResult(engine=self.name, error='已强制停止', canceled=True)

        try:
            return engine.recognize_path(path, preset=preset, max_side=max_side,
                                         timeout=timeout, cancel_event=cancel_event)
        except Exception as exc:
            # 单个子进程出问题不影响整批：还回去，下次它会被重新拉起
            return OcrResult(engine=self.name, error=f'OCR 子进程异常：{exc}')
        finally:
            if not self._stopped:
                try:
                    self._idle.put(engine)
                except Exception:
                    pass

    def recognize(self, image) -> OcrResult:
        """接口兼容：池模式只接受路径。"""
        if isinstance(image, str):
            return self.recognize_path(image)
        return OcrResult(engine=self.name,
                         error='子进程模式只支持按路径识别，请先保存图片')

    # ================================================================ 停止 / 释放

    def force_stop(self) -> bool:
        """杀掉全部子进程：所有在途识别立刻返回 canceled。"""
        self._stopped = True
        stopped = False
        for engine in list(self._engines):
            try:
                stopped = engine.force_stop() or stopped
            except Exception:
                pass
        return stopped

    def release(self) -> None:
        for engine in list(self._engines):
            try:
                engine.release()
            except Exception:
                pass

    def load(self) -> None:
        ok, error = self.start()
        if not ok:
            raise RuntimeError(error)

    def warmup(self) -> bool:
        ok, _error = self.start()
        return ok

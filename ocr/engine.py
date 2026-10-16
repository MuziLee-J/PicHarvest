"""
OCR 引擎抽象层。

设计目标（需求书第三十一节）：换 OCR 引擎不需要改扫描/复制/命名模块。

- OcrEngine：统一接口
- get_engine(prefer)：按优先级自动降级 paddle → rapid → none
- 引擎只加载一次（进程内单例），由 task_manager 串行调用，保证线程安全

所有引擎都只接受"本地图片路径或 ndarray"，绝不联网。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger('ocr')


class OcrUnavailable(RuntimeError):
    """OCR 引擎不可用（未安装 / 加载失败）。"""


@dataclass
class OcrResult:
    """统一的 OCR 输出。"""
    text: str = ''                 # 整段文本（按行拼接）
    lines: list = field(default_factory=list)      # list[str]
    scores: list = field(default_factory=list)     # list[float]
    engine: str = ''
    elapsed: float = 0.0
    error: str = ''

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.error

    @property
    def avg_confidence(self) -> float:
        valid = [s for s in self.scores if isinstance(s, (int, float)) and s > 0]
        return sum(valid) / len(valid) if valid else 0.0


# ==================================================================== 接口

class OcrEngine(ABC):
    """OCR 引擎接口。"""

    name: str = 'base'
    display_name: str = '基础引擎'

    @classmethod
    def is_available(cls) -> bool:
        """依赖是否安装。"""
        return False

    @abstractmethod
    def load(self) -> None:
        """加载模型（幂等，只做一次）。"""

    @abstractmethod
    def recognize(self, image) -> OcrResult:
        """识别单张图片。image 可以是路径(str) 或 numpy 数组。"""

    def release(self) -> None:
        """释放资源。"""

    def warmup(self) -> bool:
        """预热（触发模型加载）。返回是否成功。"""
        try:
            self.load()
            return True
        except Exception as exc:
            logger.warning('%s 预热失败：%s', self.name, exc)
            return False


class NullEngine(OcrEngine):
    """不可用时的占位引擎（明确报错，绝不偷偷上传图片）。"""

    name = 'none'
    display_name = '未启用（无可用 OCR 引擎）'

    @classmethod
    def is_available(cls) -> bool:
        return True

    def load(self) -> None:
        return

    def recognize(self, image) -> OcrResult:
        return OcrResult(engine=self.name, error='没有可用的本地 OCR 引擎，请安装 PaddleOCR 或 rapidocr_onnxruntime')


# ==================================================================== 注册与选择

#: 引擎优先级（自动模式下按顺序尝试）
_ENGINE_ORDER = ('paddle', 'rapid')

_cache: dict[str, OcrEngine] = {}
_lock = threading.RLock()


def _create_engine(key: str) -> OcrEngine:
    """按名称创建引擎实例（延迟 import，避免无依赖时崩溃）。"""
    if key == 'paddle':
        from ocr.paddle_engine import PaddleOcrEngine
        return PaddleOcrEngine()
    if key == 'rapid':
        from ocr.rapid_engine import RapidOcrEngine
        return RapidOcrEngine()
    return NullEngine()


def engine_status() -> dict:
    """返回各引擎的可用性，供界面展示。"""
    status = {}
    for key in _ENGINE_ORDER:
        try:
            engine = _create_engine(key)
            status[key] = {
                'name': engine.display_name,
                'available': type(engine).is_available(),
            }
        except Exception as exc:      # 依赖 import 失败
            status[key] = {'name': key, 'available': False, 'error': str(exc)}
    return status


def available_engine_keys() -> list[str]:
    """返回当前环境可用的引擎键列表（按优先级）。"""
    return [key for key, info in engine_status().items() if info.get('available')]


def get_engine(prefer: str = 'auto', force_reload: bool = False) -> OcrEngine:
    """获取 OCR 引擎实例（进程内单例）。

    prefer: auto | paddle | rapid | none
    """
    global _cache
    prefer = (prefer or 'auto').lower()

    with _lock:
        if prefer == 'none':
            return NullEngine()

        if prefer != 'auto':
            if not force_reload and prefer in _cache:
                return _cache[prefer]
            engine = _create_engine(prefer)
            if not type(engine).is_available():
                logger.warning('指定的引擎 %s 不可用，回退到自动选择', prefer)
                engine = None
            if engine is not None:
                _cache[prefer] = engine
                return engine

        # 自动：按优先级取第一个可用的
        for key in _ENGINE_ORDER:
            if key in _cache and not force_reload:
                return _cache[key]
            try:
                engine = _create_engine(key)
            except Exception as exc:
                logger.warning('引擎 %s 初始化失败：%s', key, exc)
                continue
            if type(engine).is_available():
                _cache[key] = engine
                logger.info('已选择 OCR 引擎：%s', engine.display_name)
                return engine

        logger.warning('没有可用的 OCR 引擎')
        return NullEngine()


def reset_engines() -> None:
    """释放所有已加载引擎（退出时调用）。"""
    with _lock:
        for engine in _cache.values():
            try:
                engine.release()
            except Exception:
                pass
        _cache.clear()


# ==================================================================== 归一化工具

def normalize_ocr_payload(payload, confidence_default: float = 0.0) -> tuple[list[str], list[float]]:
    """把各种 OCR 引擎的输出归一化成 (文本行, 置信度)。

    兼容：
    - PaddleOCR 2.x:  [[[box, (text, score)], ...]]
    - PaddleOCR 3.x:  [{'rec_texts': [...], 'rec_scores': [...]}]
    - RapidOCR:       [[box, text, score], ...]
    - 纯文本列表:     [['第一行', 0.9], ...]
    """
    texts: list[str] = []
    scores: list[float] = []

    def walk(node, depth: int = 0) -> None:
        if node is None or depth > 8:
            return
        if isinstance(node, dict):
            rec_texts = node.get('rec_texts') or node.get('texts')
            rec_scores = node.get('rec_scores') or node.get('scores')
            if isinstance(rec_texts, (list, tuple)):
                for i, text in enumerate(rec_texts):
                    score = confidence_default
                    if isinstance(rec_scores, (list, tuple)) and i < len(rec_scores):
                        try:
                            score = float(rec_scores[i])
                        except (TypeError, ValueError):
                            score = confidence_default
                    _push(text, score)
                return
            for value in node.values():
                walk(value, depth + 1)
            return
        if isinstance(node, (list, tuple)):
            # 形如 [box, text, score] 或 [box, (text, score)]
            if len(node) in (2, 3) and _is_text_nodes(node):
                _push(*_extract_text_score(node))
                return
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, str):
            _push(node, confidence_default)

    def _push(text, score) -> None:
        text = (text or '').strip() if isinstance(text, str) else ''
        if not text:
            return
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = confidence_default
        texts.append(text)
        scores.append(score)

    walk(payload)
    return texts, scores


def _is_text_nodes(node) -> bool:
    """判断是否为 [box, text(, score)] 结构。"""
    tail = node[-1]
    if isinstance(tail, str):
        return True
    if isinstance(tail, (int, float)) and len(node) >= 2 and isinstance(node[-2], str):
        return True
    if isinstance(tail, (list, tuple)) and len(tail) == 2 and isinstance(tail[0], str):
        return True
    return False


def _extract_text_score(node) -> tuple[str, float]:
    tail = node[-1]
    if isinstance(tail, str):
        return tail, 0.0
    if isinstance(tail, (int, float)):
        return str(node[-2]), float(tail)
    if isinstance(tail, (list, tuple)) and len(tail) == 2:
        try:
            return str(tail[0]), float(tail[1])
        except (TypeError, ValueError):
            return str(tail[0]), 0.0
    return '', 0.0

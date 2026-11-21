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
    boxes: list = field(default_factory=list)      # list[[x1,y1,x2,y2]] 与 lines 对齐
    engine: str = ''
    elapsed: float = 0.0
    error: str = ''
    canceled: bool = False         # 是否因用户取消/强制停止而中断
    #: 识别时所用图片的尺寸 (宽, 高)。预处理会缩放，导出训练数据时
    #: 需要用它把 boxes 映射回原图坐标。
    image_size: tuple = (0, 0)

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

    def recognize_path(self, path: str, preset: str = 'standard',
                       max_side: int = 2200, timeout: float | None = None,
                       cancel_event=None) -> OcrResult:
        """按路径识别（默认实现：本进程预处理 + 识别）。

        子进程引擎会覆盖此方法，以便把图片交给可被杀死的子进程处理。
        """
        if cancel_event is not None and cancel_event.is_set():
            return OcrResult(engine=self.name, error='已取消', canceled=True)
        try:
            from ocr.preprocess import preprocess
            image = preprocess(path, preset, max_side)
        except Exception as exc:
            return OcrResult(engine=self.name, error=f'图片读取失败：{exc}')

        result = self.recognize(image)
        try:
            height, width = image.shape[:2]
            result.image_size = (int(width), int(height))
        except Exception:
            pass
        return result

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


def _create_engine(key: str, rec_model_path: str = '', det_model_path: str = '') -> OcrEngine:
    """按名称创建引擎实例（延迟 import，避免无依赖时崩溃）。"""
    if key == 'paddle':
        from ocr.paddle_engine import PaddleOcrEngine
        return PaddleOcrEngine(rec_model_path=rec_model_path,
                               det_model_path=det_model_path)
    if key == 'rapid':
        from ocr.rapid_engine import RapidOcrEngine
        return RapidOcrEngine(rec_model_path=rec_model_path,
                              det_model_path=det_model_path)
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


def get_engine(prefer: str = 'auto', force_reload: bool = False,
               rec_model_path: str = '', det_model_path: str = '') -> OcrEngine:
    """获取 OCR 引擎实例（进程内单例）。

    prefer: auto | paddle | rapid | none
    rec_model_path / det_model_path: 可选的自定义模型路径（换更强模型用）
    """
    global _cache
    prefer = (prefer or 'auto').lower()

    with _lock:
        if prefer == 'none':
            return NullEngine()

        # 带自定义模型时不复用缓存（模型可能被换过）
        cache_key = prefer
        if rec_model_path or det_model_path:
            cache_key = f'{prefer}:{rec_model_path}:{det_model_path}'
            force_reload = True

        if prefer != 'auto':
            if not force_reload and cache_key in _cache:
                return _cache[cache_key]
            engine = _create_engine(prefer, rec_model_path, det_model_path)
            if not type(engine).is_available():
                logger.warning('指定的引擎 %s 不可用，回退到自动选择', prefer)
                engine = None
            if engine is not None:
                _cache[cache_key] = engine
                return engine

        # 自动：按优先级取第一个可用的
        # 缓存键必须带上模型路径。以前这里写的是裸键（key），于是"用自定义模型
        # 跑过一次"之后，普通 auto 调用会命中那颗自定义模型的引擎（P2-16）。
        for key in _ENGINE_ORDER:
            entry_key = (f'{key}:{rec_model_path}:{det_model_path}'
                         if (rec_model_path or det_model_path) else key)
            if entry_key in _cache and not force_reload:
                return _cache[entry_key]
            try:
                engine = _create_engine(key, rec_model_path, det_model_path)
            except Exception as exc:
                logger.warning('引擎 %s 初始化失败：%s', key, exc)
                continue
            if type(engine).is_available():
                _cache[entry_key] = engine
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

def normalize_ocr_payload(payload, confidence_default: float = 0.0
                          ) -> tuple[list[str], list[float], list[list[float]]]:
    """把各种 OCR 引擎的输出归一化成 (文本行, 置信度, 文本框)。

    兼容：
    - PaddleOCR 2.x:  [[[box, (text, score)], ...]]
    - PaddleOCR 3.x:  [{'rec_texts': [...], 'rec_scores': [...], 'rec_polys': [...]}]
    - RapidOCR:       [[box, text, score], ...]
    - 纯文本列表:     [['第一行', 0.9], ...]

    文本框统一为 [x1, y1, x2, y2]；取不到时为 None（与 lines 等长）。
    这些框用于"导出训练数据"时裁剪单行文字。
    """
    texts: list[str] = []
    scores: list[float] = []
    boxes: list = []

    def push(text, score, box=None) -> None:
        text = (text or '').strip() if isinstance(text, str) else ''
        if not text:
            return
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = confidence_default
        texts.append(text)
        scores.append(score)
        boxes.append(_normalize_box(box))

    def walk(node, depth: int = 0) -> None:
        if node is None or depth > 8:
            return
        if isinstance(node, dict):
            rec_texts = node.get('rec_texts') or node.get('texts')
            rec_scores = node.get('rec_scores') or node.get('scores')
            rec_boxes = (node.get('rec_polys') or node.get('dt_polys')
                         or node.get('boxes') or node.get('rec_boxes'))
            if isinstance(rec_texts, (list, tuple)):
                for i, text in enumerate(rec_texts):
                    score = confidence_default
                    if isinstance(rec_scores, (list, tuple)) and i < len(rec_scores):
                        try:
                            score = float(rec_scores[i])
                        except (TypeError, ValueError):
                            score = confidence_default
                    box = None
                    if isinstance(rec_boxes, (list, tuple)) and i < len(rec_boxes):
                        box = rec_boxes[i]
                    push(text, score, box)
                return
            for value in node.values():
                walk(value, depth + 1)
            return
        if isinstance(node, (list, tuple)):
            # 形如 [box, text, score] 或 [box, (text, score)]
            if len(node) in (2, 3) and _is_text_nodes(node):
                text, score = _extract_text_score(node)
                # 第一个元素是文本框；纯文本列表（[text, score]）时为 str
                candidate = node[0]
                box = candidate if isinstance(candidate, (list, tuple)) else None
                push(text, score, box)
                return
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, str):
            push(node, confidence_default)

    walk(payload)
    # 保证 boxes 与 texts 等长
    while len(boxes) < len(texts):
        boxes.append(None)
    return texts, scores, boxes[:len(texts)]


def _normalize_box(box) -> list[float] | None:
    """把各种框格式统一成 [x1, y1, x2, y2]。"""
    if box is None:
        return None
    try:
        # 四点多边形：[[x,y], [x,y], [x,y], [x,y]]
        if isinstance(box, (list, tuple)) and len(box) >= 2 and \
                isinstance(box[0], (list, tuple, tuple)):
            xs = []
            ys = []
            for point in box:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
            if xs and ys:
                return [min(xs), min(ys), max(xs), max(ys)]
        # 直接是 [x1, y1, x2, y2]
        if isinstance(box, (list, tuple)) and len(box) >= 4 and \
                all(isinstance(v, (int, float)) for v in box[:4]):
            return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
    except (TypeError, ValueError):
        return None
    return None


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

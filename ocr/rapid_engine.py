"""
RapidOCR 引擎适配（ONNXRuntime，纯本地、体积小、CPU 友好）。

作为 PaddleOCR 的轻量替代：安装体积约 100MB（PaddleOCR 通常 1GB+），
在没有 paddlepaddle 的机器上可以立即使用。

兼容两个包名：
- rapidocr_onnxruntime（旧）
- rapidocr（新，>=1.3）
"""

from __future__ import annotations

import threading
import time

from ocr.engine import OcrEngine, OcrResult, _normalize_box, normalize_ocr_payload
from utils.logger import get_logger

logger = get_logger('ocr.rapid')


class RapidOcrEngine(OcrEngine):
    """RapidOCR 本地引擎。

    支持指定自定义模型（rec_model_path / det_model_path）：
    换成 PP-OCRv4/v5 的 server 模型通常能明显提升精度，
    比从头训练划算得多。
    """

    name = 'rapid'
    display_name = 'RapidOCR（本地，轻量）'

    def __init__(self, rec_model_path: str = '', det_model_path: str = '',
                 cls_model_path: str = ''):
        self._engine = None
        self._lock = threading.RLock()
        self._api = None            # 'old' | 'new'
        self.rec_model_path = rec_model_path or ''
        self.det_model_path = det_model_path or ''
        self.cls_model_path = cls_model_path or ''

    def _model_kwargs(self) -> dict:
        kwargs = {}
        if self.rec_model_path:
            kwargs['rec_model_path'] = self.rec_model_path
        if self.det_model_path:
            kwargs['det_model_path'] = self.det_model_path
        if self.cls_model_path:
            kwargs['cls_model_path'] = self.cls_model_path
        return kwargs

    # --------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except Exception:
            pass
        try:
            import rapidocr  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
            kwargs = self._model_kwargs()
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._engine = RapidOCR(**kwargs) if kwargs else RapidOCR()
                self._api = 'old'
                logger.info('RapidOCR(rapidocr_onnxruntime) 加载完成%s',
                            f'（自定义模型：{kwargs}）' if kwargs else '')
                return
            except TypeError:
                # 旧版本可能不接受模型路径参数
                if kwargs:
                    logger.warning('当前 RapidOCR 版本不支持自定义模型路径，使用内置模型')
                self._engine = RapidOCR()
                self._api = 'old'
                return
            except Exception as exc:
                logger.warning('rapidocr_onnxruntime 加载失败：%s', exc)

            from rapidocr import RapidOCR as NewRapidOCR
            self._engine = NewRapidOCR(**kwargs) if kwargs else NewRapidOCR()
            self._api = 'new'
            logger.info('RapidOCR(rapidocr) 加载完成')

    # --------------------------------------------------

    def recognize(self, image) -> OcrResult:
        started = time.time()
        with self._lock:                     # 引擎非线程安全，串行化
            try:
                self.load()
            except Exception as exc:
                return OcrResult(engine=self.name, error=f'模型加载失败：{exc}',
                                 elapsed=time.time() - started)

            try:
                raw = self._engine(image)
            except Exception as exc:
                return OcrResult(engine=self.name, error=f'OCR 识别失败：{exc}',
                                 elapsed=time.time() - started)

            # 老版本返回 (result, elapse)；新版本返回带属性的对象
            payload = raw[0] if isinstance(raw, tuple) and len(raw) == 2 else raw
            texts, scores, boxes = self._extract(payload)

            text = '\n'.join(texts)
            if not text.strip():
                return OcrResult(engine=self.name, text='', lines=[], scores=[],
                                 boxes=[], elapsed=time.time() - started,
                                 error='未识别到任何文字')
            return OcrResult(engine=self.name, text=text, lines=texts, scores=scores,
                             boxes=boxes, elapsed=time.time() - started)

    # --------------------------------------------------

    @staticmethod
    def _extract(payload) -> tuple[list[str], list[float], list]:
        """从 RapidOCR 输出中提取文本、置信度与文本框。"""
        # 新版 rapidocr 返回对象，带有 .txts / .scores / .boxes
        for attr_texts, attr_scores in (('txts', 'scores'), ('texts', 'scores')):
            texts = getattr(payload, attr_texts, None)
            if isinstance(texts, (list, tuple)):
                scores = getattr(payload, attr_scores, None) or []
                raw_boxes = getattr(payload, 'boxes', None) or []
                out_texts = [str(t) for t in texts]
                out_scores = []
                out_boxes = []
                for i in range(len(out_texts)):
                    try:
                        out_scores.append(float(scores[i]))
                    except (IndexError, TypeError, ValueError):
                        out_scores.append(0.0)
                    out_boxes.append(_normalize_box(raw_boxes[i])
                                     if i < len(raw_boxes) else None)
                return out_texts, out_scores, out_boxes
        # 老版：[[box, text, score], ...]
        return normalize_ocr_payload(payload)

    def release(self) -> None:
        with self._lock:
            self._engine = None

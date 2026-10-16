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

from ocr.engine import OcrEngine, OcrResult, normalize_ocr_payload
from utils.logger import get_logger

logger = get_logger('ocr.rapid')


class RapidOcrEngine(OcrEngine):
    """RapidOCR 本地引擎。"""

    name = 'rapid'
    display_name = 'RapidOCR（本地，轻量）'

    def __init__(self):
        self._engine = None
        self._lock = threading.RLock()
        self._api = None            # 'old' | 'new'

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
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._engine = RapidOCR()
                self._api = 'old'
                logger.info('RapidOCR(rapidocr_onnxruntime) 加载完成')
                return
            except Exception as exc:
                logger.warning('rapidocr_onnxruntime 加载失败：%s', exc)

            from rapidocr import RapidOCR as NewRapidOCR
            self._engine = NewRapidOCR()
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
            texts, scores = self._extract(payload)

            text = '\n'.join(texts)
            if not text.strip():
                return OcrResult(engine=self.name, text='', lines=[], scores=[],
                                 elapsed=time.time() - started,
                                 error='未识别到任何文字')
            return OcrResult(engine=self.name, text=text, lines=texts, scores=scores,
                             elapsed=time.time() - started)

    # --------------------------------------------------

    @staticmethod
    def _extract(payload) -> tuple[list[str], list[float]]:
        """从 RapidOCR 输出中提取文本与置信度。"""
        # 新版 rapidocr 返回对象，带有 .txts / .scores
        for attr_texts, attr_scores in (('txts', 'scores'), ('texts', 'scores')):
            texts = getattr(payload, attr_texts, None)
            if isinstance(texts, (list, tuple)):
                scores = getattr(payload, attr_scores, None) or []
                out_texts = [str(t) for t in texts]
                out_scores = []
                for i in range(len(out_texts)):
                    try:
                        out_scores.append(float(scores[i]))
                    except (IndexError, TypeError, ValueError):
                        out_scores.append(0.0)
                return out_texts, out_scores
        # 老版：[[box, text, score], ...]
        return normalize_ocr_payload(payload)

    def release(self) -> None:
        with self._lock:
            self._engine = None

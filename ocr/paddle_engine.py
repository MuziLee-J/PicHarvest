"""
PaddleOCR 引擎适配（本地推理，绝不上传图片）。

兼容性处理：
- PaddleOCR 2.6 ~ 2.10：PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
  输出 [[ [box, (text, score)], ... ]]
- PaddleOCR 3.x（PP-OCRv5）：PaddleOCR(lang='ch')，输出形如
  [{'rec_texts': [...], 'rec_scores': [...], 'rec_polys': [...]}]

两者输出统一由 ocr.engine.normalize_ocr_payload 归一化。
"""

from __future__ import annotations

import threading
import time

from ocr.engine import OcrEngine, OcrResult, normalize_ocr_payload
from utils.logger import get_logger

logger = get_logger('ocr.paddle')


class PaddleOcrEngine(OcrEngine):
    """PaddleOCR 本地引擎。"""

    name = 'paddle'
    display_name = 'PaddleOCR（本地）'

    def __init__(self, lang: str = 'ch', use_gpu: bool = False):
        self.lang = lang or 'ch'
        self.use_gpu = use_gpu
        self._ocr = None
        self._lock = threading.RLock()
        self._warmed = False

    # --------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import paddleocr  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        """加载模型（幂等，只执行一次）。"""
        with self._lock:
            if self._ocr is not None:
                return
            from paddleocr import PaddleOCR

            attempts = [
                dict(lang=self.lang, use_angle_cls=True, show_log=False),
                dict(lang=self.lang, use_angle_cls=True),
                dict(lang=self.lang),
                dict(),
            ]
            last_error = None
            for kwargs in attempts:
                try:
                    self._ocr = PaddleOCR(**kwargs)
                    self._warmed = True
                    logger.info('PaddleOCR 模型加载完成（参数：%s）', list(kwargs.keys()))
                    return
                except (TypeError, ValueError) as exc:
                    last_error = exc
                    continue
                except Exception as exc:      # 模型下载失败 / 环境异常
                    last_error = exc
                    logger.error('PaddleOCR 初始化失败：%s', exc)
                    break
            self._ocr = None
            raise RuntimeError(f'PaddleOCR 初始化失败：{last_error}')

    # --------------------------------------------------

    def recognize(self, image) -> OcrResult:
        """识别单张图片（image 为路径或 ndarray）。"""
        started = time.time()
        with self._lock:                      # PaddleOCR 非线程安全，串行化
            try:
                self.load()
            except Exception as exc:
                return OcrResult(engine=self.name, error=f'模型加载失败：{exc}',
                                 elapsed=time.time() - started)

            if self._ocr is None:
                return OcrResult(engine=self.name, error='OCR 引擎未就绪',
                                 elapsed=time.time() - started)

            raw = None
            error = ''
            # 依次尝试不同版本的调用方式（2.x 带 cls 参数 / 3.x 不带 / predict 接口）
            callers = (
                lambda img: self._ocr.ocr(img, cls=True),
                lambda img: self._ocr.ocr(img),
                lambda img: self._ocr.predict(img),
            )
            for caller in callers:
                try:
                    raw = caller(image)
                    error = ''
                    break
                except TypeError as exc:
                    error = str(exc)
                    continue
                except AttributeError as exc:
                    error = str(exc)
                    continue
                except Exception as exc:
                    error = str(exc)
                    break

            if error and raw is None:
                return OcrResult(engine=self.name, error=f'OCR 识别失败：{error}',
                                 elapsed=time.time() - started)

            texts, scores = normalize_ocr_payload(raw)
            text = '\n'.join(texts)
            if not text.strip():
                # 空结果通常意味着图片没有文字或质量太差
                return OcrResult(engine=self.name, text='', lines=[], scores=[],
                                 elapsed=time.time() - started,
                                 error='未识别到任何文字')
            return OcrResult(engine=self.name, text=text, lines=texts, scores=scores,
                             elapsed=time.time() - started)

    # --------------------------------------------------

    def release(self) -> None:
        with self._lock:
            self._ocr = None
            self._warmed = False

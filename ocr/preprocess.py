"""
OCR 图像预处理。

铁律：**只为了 OCR 而处理，绝不回写原始图片**。
本模块只返回内存中的 numpy 数组，不产生任何磁盘写入。

预处理链路（按强度递增）：
    none     仅解码（含 EXIF 方向纠正）
    light    解码 + 缩小过大图片 + 灰度
    standard light + CLAHE 对比度增强 + 轻降噪
    strong   standard + 自动纠偏 + 锐化 + 自适应二值化

实现策略：优先用 OpenCV；若环境没有 OpenCV，则退化为 Pillow 实现，
保证在没有 cv2 的机器上依然可跑（只是效果略弱）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from utils.file_utils import ImageLoadError, load_image_rgb

#: 可选预设
PRESETS = ('none', 'light', 'standard', 'strong')
PRESET_LABELS = {
    'none': '不处理（最快）',
    'light': '轻度（缩放 + 灰度）',
    'standard': '标准（推荐：增强对比 + 降噪）',
    'strong': '强力（纠偏 + 锐化 + 二值化）',
}

#: OCR 输入的最短边下限（太小会严重掉点）
MIN_SIDE = 720


@dataclass
class PreprocessInfo:
    """预处理信息（用于日志/调试）。"""
    preset: str = 'standard'
    width: int = 0
    height: int = 0
    rotated: float = 0.0
    scaled: float = 1.0
    used_cv2: bool = False
    steps: list = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def has_cv2() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def has_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 解码

def load_array(path: str, max_side: int = 2200):
    """读取图片为 numpy 数组（RGB）。

    使用 Pillow 解码以支持中文路径；随后按需转换。
    """
    image = load_image_rgb(path)
    try:
        if not has_numpy():
            raise ImageLoadError('未安装 numpy，无法进行图像预处理')
        import numpy as np
        if max_side and max(image.size) > max_side:
            ratio = max_side / float(max(image.size))
            new_size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
            image = image.resize(new_size)
        return np.asarray(image)
    finally:
        try:
            image.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 主入口

def preprocess(path: str, preset: str = 'standard', max_side: int = 2200,
               info: PreprocessInfo | None = None):
    """读取并预处理图片，返回可直接送入 OCR 的 numpy 数组。

    失败时抛出 ImageLoadError（由上层捕获，记录为该文件失败）。
    """
    preset = (preset or 'standard').lower()
    if preset not in PRESETS:
        preset = 'standard'

    info = info or PreprocessInfo()
    info.preset = preset

    array = load_array(path, max_side if preset != 'none' else max_side)
    info.height, info.width = array.shape[:2]

    if preset == 'none':
        info.steps.append('仅解码')
        return array

    if has_cv2():
        return _pipeline_cv2(array, preset, info)
    return _pipeline_pillow(array, preset, info)


# ---------------------------------------------------------------- OpenCV 实现

def _pipeline_cv2(image, preset: str, info: PreprocessInfo):
    import cv2
    import numpy as np

    info.used_cv2 = True
    img = image

    # 1) 灰度（OCR 只需要亮度信息，同时降低内存）
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        gray = img
    info.steps.append('灰度化')

    # 2) 过小图片放大（提升小字识别率）
    h, w = gray.shape[:2]
    if min(h, w) < MIN_SIDE:
        scale = MIN_SIDE / float(max(1, min(h, w)))
        if scale > 1.05 and max(h, w) * scale <= 6000:
            gray = cv2.resize(gray, (int(w * scale), int(h * scale)),
                              interpolation=cv2.INTER_CUBIC)
            info.scaled = round(scale, 2)
            info.steps.append(f'放大 ×{scale:.2f}')

    if preset in ('standard', 'strong'):
        # 3) CLAHE 局部对比度增强
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        info.steps.append('CLAHE 对比度增强')

        # 4) 轻度降噪（保留文字边缘）
        gray = cv2.bilateralFilter(gray, d=5, sigmaColor=45, sigmaSpace=45)
        info.steps.append('双边降噪')

    if preset == 'strong':
        # 5) 自动纠偏
        angle = estimate_skew_angle(gray)
        if abs(angle) >= 0.6:
            gray = rotate_image(gray, angle)
            info.rotated = round(angle, 2)
            info.steps.append(f'纠偏 {angle:.2f}°')

        # 6) 反锐化掩膜提升笔画清晰度
        blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
        gray = cv2.addWeighted(gray, 1.6, blur, -0.6, 0)
        info.steps.append('锐化')

        # 7) 自适应二值化（对光照不均的拍照件效果明显）
        gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 31, 12)
        info.steps.append('自适应二值化')

    info.height, info.width = gray.shape[:2]
    # 转换为 3 通道（多数 OCR 引擎要求 BGR/GRAY 三通道输入）
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB) if gray.ndim == 2 else gray


# ---------------------------------------------------------------- Pillow 回退

def _pipeline_pillow(image, preset: str, info: PreprocessInfo):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    info.used_cv2 = False
    img = Image.fromarray(image) if not isinstance(image, Image.Image) else image

    h, w = img.size[1], img.size[0]
    if min(h, w) < MIN_SIDE:
        scale = MIN_SIDE / float(max(1, min(h, w)))
        if scale > 1.05 and max(h, w) * scale <= 6000:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            info.scaled = round(scale, 2)
            info.steps.append(f'放大 ×{scale:.2f}')

    gray = ImageOps.grayscale(img)
    info.steps.append('灰度化(Pillow)')

    if preset in ('standard', 'strong'):
        gray = ImageOps.autocontrast(gray, cutoff=1)
        gray = ImageEnhance.Contrast(gray).enhance(1.35)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))
        info.steps.append('自动对比度 + 中值降噪')

    if preset == 'strong':
        gray = gray.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=4))
        info.steps.append('USM 锐化')

    info.height, info.width = gray.size[1], gray.size[0]
    import numpy as np
    return np.asarray(gray.convert('RGB'))


# ---------------------------------------------------------------- 纠偏

def estimate_skew_angle(gray, max_angle: float = 12.0) -> float:
    """估计文本倾斜角度（度）。失败返回 0。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return 0.0

    try:
        # 反色后二值化，用最小外接矩形估计主方向
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = cv2.findNonZero(binary)
        if coords is None or len(coords) < 50:
            return 0.0
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        if angle < -45:
            angle += 90
        elif angle > 45:
            angle -= 90
        if abs(angle) > max_angle:
            return 0.0
        return float(angle)
    except Exception:
        return 0.0


def rotate_image(image, angle: float):
    """旋转图片（白色背景填充），保持内容完整。"""
    try:
        import cv2
        height, width = image.shape[:2]
        center = (width / 2.0, height / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
        new_w = int(height * sin + width * cos)
        new_h = int(height * cos + width * sin)
        matrix[0, 2] += new_w / 2 - center[0]
        matrix[1, 2] += new_h / 2 - center[1]
        return cv2.warpAffine(image, matrix, (new_w, new_h),
                              flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    except Exception:
        return image


# ---------------------------------------------------------------- 缩略图

def make_thumbnail(path: str, max_side: int = 220):
    """生成缩略图数组（用于界面列表），失败返回 None。"""
    try:
        from PIL import Image
        image = load_image_rgb(path)
        try:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
            import numpy as np
            return np.asarray(image)
        finally:
            image.close()
    except Exception:
        return None


def estimate_quality_hint(path: str) -> str:
    """粗略提示图片质量（用于界面提醒用户）。"""
    try:
        from PIL import Image, ImageStat
        image = load_image_rgb(path)
        try:
            gray = image.convert('L')
            stat = ImageStat.Stat(gray)
            stddev = stat.stddev[0] if stat.stddev else 0.0
            size = min(image.size)
            if size < 400:
                return f'分辨率偏低（短边 {size}px）'
            if stddev < 18:
                return '对比度偏低'
            return ''
        finally:
            image.close()
    except Exception:
        return ''

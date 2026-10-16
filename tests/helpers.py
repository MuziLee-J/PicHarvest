"""
测试公共工具：构造临时目录树、真实图片、内存记录。

所有测试都使用临时目录，绝不触碰真实数据。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from models import ImageRecord, Status

TARGET_NAME = 'SAMPLE_NA.jpg'


def has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def has_ocr() -> bool:
    try:
        from ocr.engine import available_engine_keys
        return bool(available_engine_keys())
    except Exception:
        return False


class TempTree:
    """临时目录树上下文管理器。"""

    def __init__(self, prefix: str = 'picharvest_test_'):
        self.prefix = prefix
        self.root = ''

    def __enter__(self) -> str:
        self.root = tempfile.mkdtemp(prefix=self.prefix)
        return self.root

    def __exit__(self, *exc_info) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def write_file(path: str, content: bytes = b'not-a-real-image') -> str:
    """写一个占位文件（用于扫描类测试，不需要真实图片）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(content)
    return path


def make_image(path: str, text: str = '', size=(800, 600)) -> str:
    """生成一张带文字的图片（需要 Pillow；没有则退化为占位文件）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not has_pillow():
        return write_file(path)

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf',
                      r'C:\Windows\Fonts\simsun.ttc'):
        if os.path.isfile(candidate):
            try:
                font = ImageFont.truetype(candidate, 40)
                break
            except Exception:
                font = None
    if font is None:
        font = ImageFont.load_default()

    y = 80
    for line in (text or '').split('\n'):
        draw.text((80, y), line, fill='black', font=font)
        y += 70
    image.save(path, 'JPEG', quality=92)
    return path


def make_corrupt_image(path: str) -> str:
    """生成一个无法解析的"图片"。"""
    return write_file(path, b'\xff\xd8\xff\xe0broken' + os.urandom(32))


#: 标准测试数据（对应需求书第三十三节）：(相对目录, 文字, 是否损坏)
SAMPLES = [
    ('CASE001/1440', '姓名：张三\n性别：男\n编号：CASE001', False),
    ('CASE002/1440', '姓名：张三\n性别：男', False),          # 重名测试
    ('CASE003/1440', '姓名：李四\n性别：女\n部门：质检科', False),
    ('CASE004/1440', '', False),                            # 无文字
    ('CASE005/1440', '', True),                             # 损坏文件
    ('其他/1440', '姓名：赵六', False),
    ('CASE001/2880', '姓名：张三', False),
    ('CASE002/2880', '姓名：张三', False),
    ('CASE003/2880', '姓名：李四', False),
    ('CASE006/960', '姓名：王五', False),                    # 第二层不同
    ('其他/子目录/1440', '姓名：不该被命中', False),              # 层级不符
]


def build_dataset(root: str, samples: list | None = None) -> str:
    """构造标准测试数据。

    命中 */1440/SAMPLE_NA.jpg 的应为 6 个：
        CASE001 / CASE002 / CASE003 / CASE004 / CASE005 / 其他
    不命中的：*/2880（3 个）、CASE006/960、其他/子目录/1440
    """
    for rel_dir, text, corrupt in (samples or SAMPLES):
        path = os.path.join(root, rel_dir.replace('/', os.sep), TARGET_NAME)
        if corrupt:
            make_corrupt_image(path)
        else:
            make_image(path, text)
    return root


def make_record(source_path: str, index: int = 1, source_folder: str = 'CASE001',
                rel_path: str = '', ext: str = '.jpg', size: int = 100,
                status: str = Status.READY) -> ImageRecord:
    """构造一条内存记录（不访问磁盘）。"""
    import os as _os
    return ImageRecord(
        index=index,
        source_path=source_path,
        rel_path=rel_path or f'{source_folder}/1440/{_os.path.basename(source_path)}',
        source_folder=source_folder,
        original_name=_os.path.basename(source_path),
        ext=ext,
        size=size,
        status=status,
    )

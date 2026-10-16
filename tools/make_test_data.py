"""
生成测试数据结构（用于验证路径匹配 / OCR / 复制 / 重名处理 / 异常处理）。

用法：
    python tools/make_test_data.py [目标目录]

生成结果（默认 ./_testdata/20250101）：

    20250101/
    ├── CASE001/1440/SAMPLE_NA.jpg    文字：姓名：张三
    ├── CASE001/2880/SAMPLE_NA.jpg    文字：姓名：张三
    ├── CASE002/1440/SAMPLE_NA.jpg    文字：姓名：张三（重名测试）
    ├── CASE002/2880/SAMPLE_NA.jpg
    ├── CASE003/1440/SAMPLE_NA.jpg    文字：姓名：李四
    ├── CASE003/2880/SAMPLE_NA.jpg
    ├── CASE004/1440/SAMPLE_NA.jpg    无文字（OCR 失败 → 人工确认）
    ├── CASE005/1440/SAMPLE_NA.jpg    损坏文件（错误处理）
    ├── CASE006/960/SAMPLE_NA.jpg     不同第二层（不应被 1440 规则命中）
    ├── 其他/1440/SAMPLE_NA.jpg          非匹配层级
    ├── 其他/子目录/1440/SAMPLE_NA.jpg   多一层（严格匹配下不命中）
    └── 无关文件/readme.txt                      非图片
"""

from __future__ import annotations

import os
import sys

# 允许直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGET_NAME = 'SAMPLE_NA.jpg'

#: (相对目录, 图片文字内容, 特殊标记)
SAMPLES = [
    ('CASE001/1440', '姓名：张三\n性别：男\n编号：CASE001', ''),
    ('CASE001/2880', '姓名：张三\n性别：男\n编号：CASE001', ''),
    ('CASE002/1440', '姓 名:张三\n性别:男', ''),
    ('CASE002/2880', '姓 名:张三\n性别:男', ''),
    ('CASE003/1440', '姓名：李四\n性别：女\n部门：质检科', ''),
    ('CASE003/2880', '姓名：李四\n性别：女', ''),
    ('CASE004/1440', '', 'plain'),                       # 无文字
    ('CASE005/1440', '损坏', 'corrupt'),                  # 损坏文件
    ('CASE006/960', '姓名：王五', ''),                     # 不同第二层
    ('其他/1440', '姓名：不该被命中', ''),                      # 层级不符
    ('其他/子目录/1440', '姓名：不该被命中', ''),                 # 多一层
]


def find_cjk_font() -> str | None:
    """找一个可用的中文字体。"""
    candidates = [
        r'C:\Windows\Fonts\msyh.ttc',
        r'C:\Windows\Fonts\msyhbd.ttc',
        r'C:\Windows\Fonts\simhei.ttf',
        r'C:\Windows\Fonts\simsun.ttc',
        r'C:\Windows\Fonts\Deng.ttf',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/System/Library/Fonts/PingFang.ttc',
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def make_image(path: str, text: str, corrupt: bool = False) -> None:
    """生成一张带文字的测试图片（或损坏文件）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if corrupt:
        with open(path, 'wb') as handle:
            handle.write(b'\xff\xd8\xff\xe0NOT_A_REAL_JPEG' + os.urandom(64))
        return

    from PIL import Image, ImageDraw, ImageFont

    width, height = 900, 640
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)

    font = None
    font_path = find_cjk_font()
    if font_path:
        try:
            font = ImageFont.truetype(font_path, 44)
        except Exception:
            font = None
    if font is None:
        font = ImageFont.load_default()

    y = 60
    for line in (text or '').split('\n'):
        draw.text((60, y), line, fill='black', font=font)
        y += 74

    # 加一点边框，模拟真实扫描件
    draw.rectangle([20, 20, width - 20, height - 20], outline='black', width=3)
    image.save(path, 'JPEG', quality=92)


def build(target_root: str, with_images: bool = True) -> str:
    """生成完整测试数据，返回根目录路径。"""
    root = os.path.join(target_root, '20250101')
    os.makedirs(root, exist_ok=True)

    for rel_dir, text, flag in SAMPLES:
        target = os.path.join(root, rel_dir.replace('/', os.sep), TARGET_NAME)
        if with_images:
            make_image(target, text, corrupt=(flag == 'corrupt'))
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8') as handle:
                handle.write(text)

    # 干扰文件
    noise_dir = os.path.join(root, '无关文件')
    os.makedirs(noise_dir, exist_ok=True)
    with open(os.path.join(noise_dir, 'readme.txt'), 'w', encoding='utf-8') as handle:
        handle.write('这不是图片，不应该被提取。\n')

    # 1440 目录里再放一个别的图片名，验证"按文件名匹配"
    other = os.path.join(root, 'CASE001', '1440', 'OTHER_IMAGE.jpg')
    if with_images:
        make_image(other, '姓名：不该被命中')
    else:
        with open(other, 'w', encoding='utf-8') as handle:
            handle.write('other')

    return root


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '_testdata')
    root = build(target)
    print(f'测试数据已生成：{root}')
    print('规则示例： */1440/SAMPLE_NA.jpg')
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            count += 1
    print(f'共 {count} 个文件')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

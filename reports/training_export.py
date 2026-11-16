"""
导出 OCR 训练数据。

输出的是**识别模型微调**需要的格式：单行文字截图 + 正确文本。

    训练数据_20261021_120000/
    ├── crops/
    │   ├── 0001.jpg           ← 姓名所在行的截图
    │   └── ...
    ├── train.txt              ← "crops/0001.jpg\t姓名：张三"
    ├── manifest.csv           ← 每条样本的完整来源与上下文
    ├── review.csv             ← 待人工复核清单（重要！）
    └── 说明.txt

为什么只导出"姓名所在的那一行"：
    整张页面里只有"姓名"这一行有人工校验过（你确认或修改过），
    其余行的 OCR 文本是机器自己的输出——拿它当训练标签，等于教模型
    重复自己的错误。宁可少而准。

坐标映射：
    OCR 是在预处理后的图上做的（可能缩放过），所以裁剪前要按
    ocr_image_size / 原图尺寸 做比例换算，否则框会错位。
"""

from __future__ import annotations

import csv
import os
import time

from models import ImageRecord, Status
from utils import file_utils
from utils.logger import get_logger
from utils.validators import sanitize_stem

logger = get_logger('training')

#: 裁剪时四周额外留出的像素
CROP_PADDING = 6
#: 裁剪图的最小高度（太小会被识别模型直接丢弃）
MIN_CROP_HEIGHT = 16


def _pick_line(record: ImageRecord) -> tuple[int, str, str] | None:
    """找出"名字所在的那一行"。

    返回 (行号, 行的原始文本, 用于训练的标签文本)，找不到返回 None。

    标签必须**忠实于原图文字**（全角冒号就是全角冒号），所以优先使用
    OCR 引擎的原始输出行，而不是解析时归一化过的文本 —— 否则会教模型
    把所有全角标点都输出成半角。
    """
    name = (record.extracted_name or '').strip()
    if not name or not record.ocr_lines:
        return None

    raw_lines = (record.ocr_text or '').split('\n')
    lines = raw_lines if len(raw_lines) == len(record.ocr_lines) else record.ocr_lines
    predicted = (record.ocr_predicted_name or '').strip()

    # 1) 优先找包含人工修正名的行
    for index, line in enumerate(lines):
        if name and name in line:
            return index, line, line
    # 2) 再找包含系统原始建议的行（说明名字被改过）
    if predicted and predicted != name:
        for index, line in enumerate(lines):
            if predicted in line:
                label = line.replace(predicted, name, 1)
                return index, line, label
    # 3) 名字是纯人工输入的（不在 OCR 文本里），无法定位到具体行
    return None


def _map_box_to_original(box, ocr_size, original_size) -> list[int] | None:
    """把预处理图上的框映射回原图坐标。"""
    if not box or len(box) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return None

    ocr_w, ocr_h = (int(ocr_size[0]), int(ocr_size[1])) if ocr_size else (0, 0)
    orig_w, orig_h = int(original_size[0]), int(original_size[1])
    if ocr_w > 0 and ocr_h > 0 and orig_w > 0 and orig_h > 0 \
            and (ocr_w != orig_w or ocr_h != orig_h):
        sx = orig_w / float(ocr_w)
        sy = orig_h / float(ocr_h)
        x1, x2 = x1 * sx, x2 * sx
        y1, y2 = y1 * sy, y2 * sy

    x1 = max(0, int(x1) - CROP_PADDING)
    y1 = max(0, int(y1) - CROP_PADDING)
    x2 = min(orig_w or int(x2), int(x2) + CROP_PADDING)
    y2 = min(orig_h or int(y2), int(y2) + CROP_PADDING)
    if x2 - x1 < 4 or y2 - y1 < MIN_CROP_HEIGHT:
        return None
    return [x1, y1, x2, y2]


def export_training_data(records: list[ImageRecord], out_dir: str,
                         task_id: str | int = '') -> dict:
    """导出训练数据，返回统计信息。"""
    stamp = time.strftime('%Y%m%d_%H%M%S')
    root = os.path.join(out_dir, f'训练数据_{stamp}')
    crops_dir = os.path.join(root, 'crops')
    file_utils.ensure_dir(crops_dir)

    stats = {'total': 0, 'exported': 0, 'no_box': 0, 'no_line': 0,
             'no_text': 0, 'errors': 0, 'root': root}

    train_lines: list[str] = []
    manifest_rows: list[list] = []
    review_rows: list[list] = []

    try:
        from PIL import Image
    except ImportError:
        Image = None

    for record in records:
        if record.status in (Status.FAILED, Status.CANCELED) or not record.ocr_text:
            stats['no_text'] += 1
            continue
        stats['total'] += 1

        picked = _pick_line(record)
        if picked is None:
            stats['no_line'] += 1
            review_rows.append([record.index, record.source_path, '',
                                record.extracted_name,
                                '名字未出现在任何 OCR 行中，无法定位裁剪区域'])
            continue

        line_index, line_text, label = picked
        box = (record.ocr_boxes[line_index]
               if line_index < len(record.ocr_boxes) else None)
        if not box:
            stats['no_box'] += 1
            review_rows.append([record.index, record.source_path, label,
                                record.extracted_name, '该行没有可用的文本框'])
            continue

        original_size = file_utils.get_image_size(record.source_path)
        if not original_size:
            stats['errors'] += 1
            continue

        mapped = _map_box_to_original(box, record.ocr_image_size, original_size)
        if mapped is None:
            stats['no_box'] += 1
            review_rows.append([record.index, record.source_path, label,
                                record.extracted_name, '文本框换算后过小'])
            continue

        crop_name = f'{record.index:05d}.jpg'
        crop_path = os.path.join(crops_dir, crop_name)
        try:
            with Image.open(record.source_path) as image:
                image = image.convert('RGB')
                crop = image.crop(tuple(mapped))
                crop.save(crop_path, 'JPEG', quality=95)
        except Exception as exc:
            stats['errors'] += 1
            logger.warning('裁剪失败 %s：%s', record.source_path, exc)
            continue

        stats['exported'] += 1
        train_lines.append(f'crops/{crop_name}\t{label}')
        manifest_rows.append([
            record.index, record.source_path, record.source_folder,
            line_text, label, record.extracted_name,
            record.ocr_predicted_name, record.extract_method,
            f'{mapped[0]},{mapped[1]},{mapped[2]},{mapped[3]}',
            '已人工修正' if record.extract_method == 'manual' else '按建议采纳',
        ])
        review_rows.append([record.index, record.source_path, label,
                            record.extracted_name, ''])

    # ---------------- 写文件

    train_path = os.path.join(root, 'train.txt')
    with open(train_path, 'w', encoding='utf-8', newline='') as handle:
        handle.write('\n'.join(train_lines))

    manifest_path = os.path.join(root, 'manifest.csv')
    with open(manifest_path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['序号', '来源路径', '来源文件夹', 'OCR识别行',
                         '训练标签', '最终名称', '系统建议', '提取方式',
                         '裁剪框(原图坐标)', '校验来源'])
        writer.writerows(manifest_rows)

    review_path = os.path.join(root, 'review.csv')
    with open(review_path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['序号', '来源路径', '训练标签', '最终名称', '未导出原因'])
        writer.writerows(review_rows)

    readme_path = os.path.join(root, '说明.txt')
    with open(readme_path, 'w', encoding='utf-8') as handle:
        handle.write(README_TEMPLATE.format(
            exported=stats['exported'], total=stats['total']))

    stats['train_file'] = train_path
    stats['manifest'] = manifest_path
    stats['review'] = review_path
    stats['readme'] = readme_path
    return stats


README_TEMPLATE = """OCR 训练数据
====================================================================

本次导出：{exported} 条样本（候选 {total} 条）

文件说明
--------------------------------------------------------------------
crops/        单行文字截图，文件名与 train.txt 对应
train.txt     训练清单，格式：图片路径<TAB>正确文本
              （PaddleOCR 识别模型微调用的就是这个格式）
manifest.csv  每条样本的完整来源、OCR 结果、裁剪坐标
review.csv    全部候选清单，请在训练前人工复核"训练标签"一列

为什么只有姓名那一行
--------------------------------------------------------------------
整张页面里只有姓名行被人工校验过；其余行的文字是 OCR 自己的输出，
拿它当标签等于教模型重复自己的错误，所以没有导出。

训练前必须做的事
--------------------------------------------------------------------
1. 打开 review.csv，逐条核对"训练标签"。有错的直接改掉。
2. 删除 crops/ 里对应被删行的图片，并同步 from train.txt 删除该行。
3. 样本量建议：≥ 500 条、且覆盖真实的字体/拍摄条件。
   几十条数据微调几乎必然过拟合，效果可能反而变差。

微调步骤（PaddleOCR）
--------------------------------------------------------------------
1. 安装：pip install paddlepaddle-gpu paddleocr
2. 准备字符字典：从 PaddleOCR 的 ppocr/utils/ppocr_keys_v1.txt 复制，
   确保你的标签里出现的字符都在字典中。
3. 修改识别配置文件里的 train_list / val_list 指向本目录的 train.txt，
   并设置 character_dict_path。
4. 执行训练：
       python tools/train.py -c configs/rec/PP-OCRv4/ch_PP-OCRv4_rec.yml
5. 导出推理模型后，在本程序的"OCR 设置 → 自定义识别模型"里
   填入导出目录，即可直接使用新模型。

提醒
--------------------------------------------------------------------
如果只是想提升识别率，先试这两件事，成本低得多：
  · 换更强的预训练模型（PP-OCRv4/v5 server 版），不用训练
  · 调整预处理强度（设置里改"图像预处理"）
详见 docs/OCR_训练指南.md
"""

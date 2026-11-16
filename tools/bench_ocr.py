"""
OCR 速度基准：同一批图片，串行 vs 多进程并发的墙钟时间。

用法：
    python tools/bench_ocr.py                 # 8 张图，对比 workers=1/2/4
    python tools/bench_ocr.py -n 12 -w 1,4
    python tools/bench_ocr.py --json bench.json

说明：
- 用真实的 OCR 子进程 + 真实的预处理，数字可以直接对用户讲；
- 图片在临时目录里生成，跑完自动删除，不碰用户的任何数据；
- 每张图内容相同（都是同一份表单），避免因识别率差异影响计时。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.task_manager import TaskManager                    # noqa: E402
from models import (ExtractRule, ImageRecord, NamingConfig, OcrConfig,  # noqa: E402
                    OutputConfig, Status, TaskConfig)

LINES = [
    '姓名：张三',
    '性别：男',
    '编号：CASE001',
    '公司名称：示例科技有限公司',
    '产品名称：高强度螺栓',
    '合同编号：HT-2025-001',
    '日期：2025-01-01',
    '地址：上海市浦东新区示例路 100 号',
]


def build_images(base: str, count: int) -> list[str]:
    """生成 count 张内容相同的表单图，返回路径列表。"""
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for candidate in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.isfile(candidate):
            try:
                font = ImageFont.truetype(candidate, 34)
                break
            except Exception:
                font = None
    if font is None:
        font = ImageFont.load_default()

    paths = []
    for i in range(count):
        folder = os.path.join(base, '示例数据', f'CASE{i + 1:03d}', '1440')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'SAMPLE_NA.jpg')
        image = Image.new('RGB', (1400, 1000), 'white')
        draw = ImageDraw.Draw(image)
        for row, text in enumerate(LINES):
            draw.text((70, 60 + row * 110), text, fill='black', font=font)
        image.save(path, 'JPEG', quality=92)
        paths.append(path)
    return paths


def make_records(paths: list[str], root: str) -> list[ImageRecord]:
    records = []
    for index, path in enumerate(paths, start=1):
        rel = os.path.relpath(path, root).replace('\\', '/')
        parts = rel.split('/')
        records.append(ImageRecord(
            index=index, source_path=path, rel_path=rel,
            source_folder=parts[0] if parts else '',
            original_name='SAMPLE_NA.jpg', ext='.jpg',
            status=Status.PENDING))
    return records


def run_once(base: str, paths: list[str], workers: int) -> dict:
    config = TaskConfig(
        rule=ExtractRule(root_dir=os.path.join(base, '示例数据'), mode='advanced',
                         template='*/1440/SAMPLE_NA.jpg'),
        naming=NamingConfig(fields=['姓名']),
        ocr=OcrConfig(workers=workers, use_process=True, timeout_sec=120),
        output=OutputConfig(output_dir=os.path.join(base, 'out')),
    )
    manager = TaskManager(config)
    try:
        manager.records = make_records(paths, os.path.join(base, '示例数据'))
        started = time.time()
        summary = manager.run_ocr()
        elapsed = time.time() - started
        return {
            'workers': workers,
            'elapsed': round(elapsed, 2),
            'ok': summary.ocr_ok,
            'per_image': round(elapsed / max(1, summary.ocr_ok), 2),
            'engine': manager.engine_name,
        }
    finally:
        manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='OCR 并发基准')
    parser.add_argument('-n', '--images', type=int, default=8, help='图片数量（默认 8）')
    parser.add_argument('-w', '--workers', default='1,4',
                        help='要对比的并发数，逗号分隔（默认 1,2,4）')
    parser.add_argument('--json', default='', help='把结果另存为 JSON')
    parser.add_argument('--keep', action='store_true', help='保留临时图片目录')
    args = parser.parse_args()

    base = tempfile.mkdtemp(prefix='picharvest_bench_')
    try:
        paths = build_images(base, max(1, args.images))
        print(f'图片数量：{len(paths)}　临时目录：{base}')

        results = []
        for value in [w.strip() for w in args.workers.split(',') if w.strip()]:
            print(f'\n--- workers={value} ---')
            result = run_once(base, paths, int(value))
            results.append(result)
            print(f"耗时 {result['elapsed']}s　成功 {result['ok']} 张　"
                  f"单张均摊 {result['per_image']}s")

        baseline = next((r['elapsed'] for r in results if r['workers'] == 1), None)
        if baseline:
            print('\n并发加速比（相对 workers=1）：')
            for result in results:
                speedup = baseline / result['elapsed'] if result['elapsed'] else 0
                print(f"  workers={result['workers']}: {speedup:.2f}×")

        if args.json:
            with open(args.json, 'w', encoding='utf-8') as handle:
                json.dump(results, handle, ensure_ascii=False, indent=2)
            print(f'\n已写入 {args.json}')
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())

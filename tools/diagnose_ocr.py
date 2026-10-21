#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 诊断工具：先搞清楚"识别弱"弱在哪一环，再决定要不要训练。

它会做三件事：
    1. 对同一张图用 4 种预处理强度各识别一次，比较文本与置信度
       —— 如果某个强度明显更好，说明问题在图像质量，不在模型
    2. 打印每行文本 + 置信度
       —— 如果文字本身读对了、只是名字没选中，问题在命名逻辑（不是 OCR）
    3. 汇总推荐：当前图片最适合哪种预处理

用法：
    python tools/diagnose_ocr.py --root "D:/示例数据/20250101" --limit 5
    python tools/diagnose_ocr.py --image "D:/x.jpg"
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import NamingConfig
from ocr.engine import get_engine
from ocr.parser import parse
from ocr.preprocess import PRESETS
from ocr.suggester import suggest_for_text
from core.path_matcher import PathTemplate
from core.scanner import Scanner


def collect_images(args) -> list[str]:
    if args.image:
        return [args.image]
    scanner = Scanner(args.root, args.template or '**/*.jpg', only_images=True)
    result = scanner.scan()
    paths = [record.source_path for record in result.files]
    return paths[:args.limit] if args.limit else paths


def run_one(engine, path: str, preset: str) -> dict:
    started = time.time()
    result = engine.recognize_path(path, preset=preset, timeout=args_timeout())
    elapsed = time.time() - started
    return {
        'preset': preset,
        'text': result.text or '',
        'confidence': result.avg_confidence,
        'elapsed': elapsed,
        'error': result.error or '',
        'lines': result.lines or [],
        'scores': result.scores or [],
    }


def args_timeout() -> int:
    return 120


def main() -> int:
    parser = argparse.ArgumentParser(description='OCR 识别效果诊断')
    parser.add_argument('--root', help='图片根目录')
    parser.add_argument('--template', default='**/*.jpg', help='路径模板')
    parser.add_argument('--image', help='只诊断单张图片')
    parser.add_argument('--limit', type=int, default=3, help='最多诊断几张（默认 3）')
    parser.add_argument('--field', default='姓名', help='命名字段（默认 姓名）')
    parser.add_argument('--engine', default='auto', help='auto | rapid | paddle')
    parser.add_argument('--csv', help='把结果写成 CSV')
    args = parser.parse_args()

    if not args.image and not args.root:
        parser.error('请提供 --root 或 --image')

    paths = collect_images(args)
    if not paths:
        print('没有找到图片')
        return 1
    if not args.image:
        print(f'共 {len(paths)} 张（本次诊断前 {len(paths)} 张）')

    engine = get_engine(args.engine)
    engine.load()
    print(f'引擎：{engine.display_name}\n')

    naming = NamingConfig(fields=[args.field])
    rows = []
    best_counts: dict[str, int] = {}

    for path in paths:
        print('=' * 74)
        print(f'图片：{path}')
        print(f'尺寸：{image_size_text(path)}')
        print('-' * 74)

        results = []
        for preset in PRESETS:
            info = run_one(engine, path, preset)
            results.append(info)
            text_brief = (info['text'] or '(无文字)').replace('\n', ' | ')[:60]
            flag = '' if not info['error'] else f"  ⚠ {info['error'][:30]}"
            print(f'  {preset:<9} 置信度 {info["confidence"]:.2f}  '
                  f'{info["elapsed"]:5.2f}s  {text_brief}{flag}')

        # 选置信度最高的（相同则取文本更长的）
        valid = [r for r in results if r['text'].strip()]
        if not valid:
            print('  → 四种预处理都没有识别出文字。这是纯图像问题：'
                  '检查分辨率、对焦、光照，或该区域根本没有文字。')
            rows.append([path, '', '', 0, '', '未识别'])
            continue

        best = max(valid, key=lambda r: (r['confidence'], len(r['text'])))
        best_counts[best['preset']] = best_counts.get(best['preset'], 0) + 1

        print(f'  → 最佳预处理：{best["preset"]}（置信度 {best["confidence"]:.2f}）')

        # 逐行展示
        print('  OCR 明细：')
        for line, score in zip(best['lines'], best['scores']):
            print(f'     [{score:.2f}] {line}')

        # 命名分析
        parsed = parse(best['text'], naming.fields, best['scores'])
        suggestion = suggest_for_text(best['text'], naming, best['scores'])
        hit = parsed.fields.get(args.field)
        print(f'  命名字段「{args.field}」：'
              + (f'命中「{hit.value}」（{hit.reason}）' if hit and hit.ok
                 else '未命中'))
        print(f'  最终建议：{suggestion.name or "(无)"}'
              f'（方式={suggestion.method or "-"}，'
              f'置信度={suggestion.confidence:.2f}，'
              f'{"可靠" if suggestion.reliable else "需人工确认"}）')
        if not suggestion.name:
            print('     ↳ 文字读出来了但没选中名字：问题在命名规则，'
                  '不是 OCR。可以换个"命名字段"，或在第 3 步手工改一次'
                  '（改完会记入纠正记忆）。')

        rows.append([path, best['preset'], best['text'][:200],
                     round(best['confidence'], 3), suggestion.name,
                     '' if suggestion.name else '命名失败'])

    # ---------------- 汇总
    print('=' * 74)
    print('汇总')
    print('-' * 74)
    if best_counts:
        ranked = sorted(best_counts.items(), key=lambda item: -item[1])
        for preset, count in ranked:
            print(f'  {preset:<9} 在 {count} 张图上效果最好')
        top = ranked[0][0]
        print(f'\n建议：把"图像预处理"设为「{top}」')
        print('若仍不理想，优先换更强的预训练模型（设置里填"识别模型"路径），'
              '而不是自己训练。')
    else:
        print('  所有图片都没有识别出文字 —— 先检查图片本身。')

    if args.csv:
        with open(args.csv, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['图片', '最佳预处理', 'OCR文本', '置信度',
                             '建议名称', '问题'])
            writer.writerows(rows)
        print(f'\n明细已保存：{args.csv}')

    return 0


def image_size_text(path: str) -> str:
    try:
        from PIL import Image
        with Image.open(path) as image:
            return f'{image.width} × {image.height}'
    except Exception:
        return '未知'


if __name__ == '__main__':
    raise SystemExit(main())

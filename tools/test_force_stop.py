#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 OCR 子进程的强制停止与超时是否"真的生效"。

用法：
    python tools/test_force_stop.py

会依次验证：
    1. 子进程能启动并正常识别
    2. 取消事件能在 ~0.5 秒内终止正在进行的识别（而不是等它跑完）
    3. 超时能强制终止
    4. 被杀之后能自动重启并继续工作
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr.process_engine import ProcessOcrEngine


def make_image(path: str, text: str = '姓名：张三') -> None:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (1400, 900), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for candidate in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.isfile(candidate):
            try:
                font = ImageFont.truetype(candidate, 56)
                break
            except Exception:
                pass
    draw.text((80, 120), text, fill='black', font=font)
    # 加一些噪点，让单张识别多花一点时间
    for i in range(0, 1400, 7):
        draw.line([(i, 900 - (i % 60)), (i, 900)], fill=(230, 230, 230), width=1)
    image.save(path, 'JPEG', quality=95)


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir = os.path.join(base, '_testdata', '_force_stop')
    os.makedirs(temp_dir, exist_ok=True)
    image_path = os.path.join(temp_dir, 'sample.jpg')
    make_image(image_path)

    engine = ProcessOcrEngine(prefer='auto', startup_timeout=120)
    failures = []

    # ---------------------------------------------------------------- 1
    print('[1] 启动子进程…')
    started = time.time()
    ok, error = engine.start()
    startup = time.time() - started
    if not ok:
        print(f'    ✘ 启动失败：{error}')
        return 1
    print(f'    ✔ 就绪（{startup:.1f}s，{engine.engine_label}）')

    print('[2] 正常识别一张…')
    started = time.time()
    result = engine.recognize_path(image_path, preset='standard', timeout=60)
    elapsed = time.time() - started
    if not result.text.strip():
        print(f'    ✘ 没识别出文字：{result.error}')
        failures.append('正常识别')
    else:
        print(f'    ✔ {elapsed:.2f}s，识别到：{result.text.strip()[:40]!r}'
              f'，{len(result.boxes)} 个文本框')

    # ---------------------------------------------------------------- 2
    print('[3] 测试取消：发出识别请求后 0.2 秒触发取消…')
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.time()
    result = engine.recognize_path(image_path, preset='strong', timeout=120,
                                   cancel_event=cancel)
    elapsed = time.time() - started
    if result.canceled:
        print(f'    ✔ 在 {elapsed:.2f}s 内停止（canceled=True）')
        if elapsed > 1.5:
            failures.append(f'取消耗时过长：{elapsed:.2f}s')
    else:
        print(f'    ✘ 没有停止：canceled={result.canceled}, error={result.error!r}')
        failures.append('取消未生效')

    print('[4] 确认子进程确实已被杀死…')
    if engine.is_alive():
        print('    ✘ 进程仍然存活')
        failures.append('进程未被杀死')
    else:
        print('    ✔ 子进程已终止')

    # ---------------------------------------------------------------- 3
    print('[5] 测试超时：只给 0.01 秒…')
    started = time.time()
    result = engine.recognize_path(image_path, preset='strong', timeout=0.01)
    elapsed = time.time() - started
    if '超时' in (result.error or '') and elapsed < 2.0:
        print(f'    ✔ {elapsed:.2f}s 后超时终止：{result.error}')
    else:
        print(f'    ✘ 超时未生效：elapsed={elapsed:.2f}s, error={result.error!r}')
        failures.append('超时未生效')

    # ---------------------------------------------------------------- 4
    print('[6] 测试杀后自动重启并继续工作…')
    started = time.time()
    result = engine.recognize_path(image_path, preset='standard', timeout=90)
    if result.text.strip():
        print(f'    ✔ 重启成功（{time.time() - started:.1f}s），识别到：'
              f'{result.text.strip()[:30]!r}')
    else:
        print(f'    ✘ 重启后无法识别：{result.error}')
        failures.append('重启失败')

    engine.release()
    print()
    if failures:
        print(f'✘ 失败项：{", ".join(failures)}')
        return 1
    print('✅ 强制停止 / 超时 / 重启 全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

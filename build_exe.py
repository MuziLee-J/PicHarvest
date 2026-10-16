#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打包成 Windows 可执行程序。

用法：
    python build_exe.py              # 完整流程（建议在干净的虚拟环境里跑）
    python build_exe.py --zip        # 打包后再压缩成 zip 便于分发
    python build_exe.py --check      # 只检查环境

它会：
    1. 检查 Python 版本与依赖
    2. 运行一次自检（核心模块能否导入）
    3. 调用 PyInstaller
    4. 输出 dist/PicHarvest/PicHarvest.exe
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, 'dist')
APP_NAME = 'PicHarvest'


def log(message: str) -> None:
    print(f'[打包] {message}', flush=True)


def check_environment() -> bool:
    ok = True
    version = sys.version_info
    log(f'Python：{sys.version.split()[0]}')
    if version < (3, 9):
        log('✘ 需要 Python 3.9 及以上')
        ok = False
    elif version >= (3, 13):
        log('⚠ 建议使用 Python 3.11/3.12：PaddleOCR/RapidOCR 在 3.13+ 支持不完整')

    for module, label, required in (
        ('PySide6', 'PySide6（界面）', True),
        ('PIL', 'Pillow（图像）', True),
        ('numpy', 'numpy', True),
        ('PyInstaller', 'PyInstaller（打包）', True),
        ('rapidocr_onnxruntime', 'RapidOCR（轻量 OCR）', False),
        ('paddleocr', 'PaddleOCR（高精度 OCR）', False),
    ):
        try:
            __import__(module)
            log(f'✔ {label}')
        except ImportError:
            log(f'{"✘" if required else "○"} {label}（未安装）')
            if required:
                ok = False
    return ok


def self_test() -> bool:
    """打包前自检：核心模块必须能导入。"""
    log('运行核心模块自检…')
    code = (
        'import sys; sys.path.insert(0, r"%s");'
        'import core.task_manager, core.scanner, core.filename_manager,'
        'ocr.parser, ocr.suggester, database.database, reports.report_manager,'
        'models, utils.validators;'
        'print("core-ok")' % BASE_DIR
    )
    result = subprocess.run([sys.executable, '-c', code],
                            capture_output=True, text=True)
    if 'core-ok' not in result.stdout:
        log('✘ 核心模块导入失败：')
        print(result.stdout, result.stderr)
        return False
    log('✔ 核心模块正常')

    try:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        code = ('import sys; sys.path.insert(0, r"%s");'
                'from PySide6.QtWidgets import QApplication;'
                'app=QApplication([]);'
                'from ui.main_window import MainWindow;'
                'w=MainWindow(); print("ui-ok")' % BASE_DIR)
        result = subprocess.run([sys.executable, '-c', code],
                                capture_output=True, text=True)
        if 'ui-ok' not in result.stdout:
            log('⚠ 界面自检未通过（不影响打包，但请检查）：')
            print(result.stdout[-2000:], result.stderr[-2000:])
        else:
            log('✔ 界面模块正常')
    except Exception as exc:
        log(f'⚠ 界面自检跳过：{exc}')
    return True


def clean() -> None:
    for path in (os.path.join(BASE_DIR, 'build'), DIST_DIR):
        if os.path.isdir(path):
            log(f'清理 {path}')
            shutil.rmtree(path, ignore_errors=True)


def build() -> bool:
    spec = os.path.join(BASE_DIR, 'packaging', 'PicHarvest.spec')
    if not os.path.isfile(spec):
        log(f'✘ 找不到 {spec}')
        return False

    command = [sys.executable, '-m', 'PyInstaller', spec,
               '--noconfirm', '--clean',
               '--distpath', DIST_DIR,
               '--workpath', os.path.join(BASE_DIR, 'build')]
    log('开始打包（首次可能需要 3-10 分钟）…')
    log(' '.join(command))
    result = subprocess.run(command, cwd=BASE_DIR)
    if result.returncode != 0:
        log('✘ 打包失败，请查看上面 PyInstaller 的输出')
        return False

    exe_path = os.path.join(DIST_DIR, APP_NAME, f'{APP_NAME}.exe')
    if not os.path.isfile(exe_path):
        log(f'✘ 没有找到产物 {exe_path}')
        return False

    size = sum(
        os.path.getsize(os.path.join(dirpath, name))
        for dirpath, _dirs, files in os.walk(os.path.join(DIST_DIR, APP_NAME))
        for name in files
    )
    log(f'✔ 打包完成：{exe_path}')
    log(f'  目录总大小：{size / 1024 / 1024:.1f} MB')
    log('  分发方式：把 dist/PicHarvest 整个文件夹拷给用户，双击 PicHarvest.exe')
    return True


def make_zip() -> str | None:
    """压缩成 zip（便于拷贝到其他电脑使用）。"""
    folder = os.path.join(DIST_DIR, APP_NAME)
    if not os.path.isdir(folder):
        return None

    # 把面向最终用户的使用说明放进根目录，跟着 exe 一起分发
    readme = os.path.join(BASE_DIR, 'packaging', '使用说明.txt')
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(folder, '使用说明.txt'))

    zip_path = os.path.join(DIST_DIR, f'{APP_NAME}.zip')
    log(f'压缩到 {zip_path} …（约 300MB，需要 1-3 分钟）')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for dirpath, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(dirpath, name)
                archive.write(full, os.path.relpath(full, DIST_DIR))
    log(f'✔ 完成：{zip_path}（{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB）')
    log('  拷贝到其他电脑后：解压 → 双击 PicHarvest\\PicHarvest.exe')
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description='打包成 Windows exe')
    parser.add_argument('--check', action='store_true', help='只检查环境')
    parser.add_argument('--zip', action='store_true', help='打包后压缩成 zip')
    parser.add_argument('--keep-build', action='store_true', help='不清理 build 目录')
    args = parser.parse_args()

    if os.name != 'nt':
        log('⚠ 当前不是 Windows，PyInstaller 产物将无法在 Windows 上直接运行')

    if not check_environment():
        log('请先安装依赖：pip install -r requirements.txt')
        return 1
    if not self_test():
        return 1
    if args.check:
        return 0

    if not args.keep_build:
        clean()
    if not build():
        return 1
    if args.zip:
        make_zip()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

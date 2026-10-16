# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置。

用法（在项目根目录）：
    pyinstaller packaging/PicHarvest.spec --noconfirm --clean

产物：
    dist/PicHarvest/PicHarvest.exe   ← 双击即可运行（onedir 模式，启动快）
    dist/PicHarvest.zip              ← 打包脚本会自动压缩，便于分发

说明：
- 采用 onedir（目录）模式而不是 onefile：OCR 与 Qt 体积大，
  onefile 每次启动都要解压到临时目录，首启动会慢十几秒。
- PaddleOCR 默认不打进包里（体积 1GB+），需要时见文件末尾说明。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

datas = []
binaries = []
hiddenimports = [
    'PIL', 'PIL.Image', 'PIL.ImageOps', 'PIL.ImageDraw', 'PIL.ImageFont',
    'sqlite3', 'csv', 'json',
]

# ------------------------------------------------------------------ OCR 引擎

def add_package(name: str, required: bool = False) -> bool:
    """把整个包（含模型/二进制）打进去。"""
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(name)
    except Exception as exc:
        if required:
            raise
        print(f'[spec] 跳过 {name}：{exc}')
        return False
    datas.extend(pkg_datas)
    binaries.extend(pkg_binaries)
    hiddenimports.extend(pkg_hidden)
    print(f'[spec] 已包含 {name}（数据 {len(pkg_datas)} 项）')
    return True


# 轻量 OCR：默认打包（约 100MB，含 onnxruntime 与模型）
add_package('rapidocr_onnxruntime')
add_package('onnxruntime')

# 精度更高的 PaddleOCR：默认不打（体积大），需要时把下面两行取消注释
# add_package('paddleocr')
# add_package('paddle')

for optional in ('openpyxl', 'send2trash'):
    add_package(optional)

# ------------------------------------------------------------------ 入口

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 用不到的 Qt 模块，剔除后可显著减小体积
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.Qt3DCore',
        'PySide6.QtMultimedia', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
        'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtOpenGL',
        # 用不到的科学计算栈（PaddleOCR 若打包则不要排除 numpy/scipy）
        'matplotlib', 'pandas', 'IPython', 'tkinter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PicHarvest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 常被杀毒误报，默认关闭
    console=False,             # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon=os.path.join(PROJECT_ROOT, 'packaging', 'app.ico'),  # 有图标时取消注释
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PicHarvest',
)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片智能提取与 OCR 自动命名工具 —— 程序入口。

启动方式：
    python main.py

打包后直接双击 exe 即可运行。
"""

from __future__ import annotations

import os
import sys
import traceback

# ---------------------------------------------------------------- 路径准备

def app_base_dir() -> str:
    """程序根目录（兼容 PyInstaller 打包后的运行环境）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = app_base_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


# ---------------------------------------------------------------- 环境准备

def prepare_environment() -> None:
    """设置运行环境（在导入 Qt 之前完成）。"""
    # 高 DPI 支持（Qt6 默认开启，这里只设置缩放策略）
    os.environ.setdefault('QT_ENABLE_HIGHDPI_SCALING', '1')
    # 让 PaddleOCR 在无 GPU 环境下安静一些
    os.environ.setdefault('FLAGS_call_stack_level', '2')
    os.environ.setdefault('GLOG_minloglevel', '2')
    # 禁止任何隐式联网行为
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')


def install_excepthook() -> None:
    """全局异常兜底：写日志并在界面提示，而不是直接崩溃退出。"""
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            from utils.logger import get_logger
            get_logger('crash').error('未捕获异常：\n%s', text)
        except Exception:
            print(text, file=sys.stderr)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, '程序遇到问题',
                    f'发生未预期的错误，程序会尽量继续运行。\n\n'
                    f'{exc_type.__name__}: {exc_value}\n\n'
                    f'详细信息已写入日志目录：\n'
                    f'{os.path.join(os.environ.get("LOCALAPPDATA", ""), "PicHarvest", "logs")}')
        except Exception:
            pass

    sys.excepthook = hook


# ---------------------------------------------------------------- 自检

def run_selftest() -> int:
    """打包后自检：核心模块、图像处理、OCR 引擎、临时文件读写。

    用法：PicHarvest.exe --selftest
    结果写入 %LOCALAPPDATA%/PicHarvest/selftest_report.txt 并作为退出码返回
    （0 = 全部通过）。打包成无控制台程序后看不到 stdout，所以写文件。
    """
    lines: list[str] = []
    problems: list[str] = []

    def check(label: str, func):
        try:
            value = func()
            lines.append(f'[OK]   {label}：{value}')
            return value
        except Exception as exc:
            lines.append(f'[FAIL] {label}：{type(exc).__name__}: {exc}')
            problems.append(label)
            return None

    lines.append(f'自检时间：{__import__("time").strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'程序目录：{BASE_DIR}')
    lines.append(f'Python：{sys.version.split()[0]}　已打包：{getattr(sys, "frozen", False)}')
    lines.append('-' * 60)

    check('导入 Qt', lambda: __import__('PySide6.QtCore', fromlist=['Qt']).__name__)
    check('导入 Pillow', lambda: __import__('PIL', fromlist=['Image']).__version__)
    check('导入 numpy', lambda: __import__('numpy').__version__)
    check('导入核心模块', lambda: __import__('core.path_matcher',
                                              fromlist=['PathTemplate']).PathTemplate(
        '*/1440/a.jpg').match('CASE101/1440/a.jpg'))
    check('导入数据库', lambda: __import__('database.database',
                                            fromlist=['Database']).Database)
    check('导入报告模块', lambda: __import__('reports.report_manager',
                                             fromlist=['ReportManager']).ReportManager)

    # 图像与 OCR（在临时目录里造一张图，不碰用户数据）
    temp_dir = None
    try:
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix='picharvest_selftest_')

        def make_image():
            from PIL import Image, ImageDraw, ImageFont
            path = os.path.join(temp_dir, 'selftest.jpg')
            image = Image.new('RGB', (800, 400), 'white')
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default()
            for candidate in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
                if os.path.isfile(candidate):
                    try:
                        font = ImageFont.truetype(candidate, 44)
                        break
                    except Exception:
                        pass
            draw.text((60, 120), '姓名：张三', fill='black', font=font)
            image.save(path, 'JPEG', quality=92)
            return path

        image_path = check('生成测试图片', make_image)

        check('OCR 预处理', lambda: __import__('ocr.preprocess',
                                               fromlist=['preprocess']).preprocess(
            image_path, 'standard').shape)

        def run_ocr():
            from ocr.engine import get_engine
            engine = get_engine('auto')
            result = engine.recognize(__import__('ocr.preprocess', fromlist=['preprocess'])
                                      .preprocess(image_path, 'standard'))
            if not result.text.strip():
                raise RuntimeError(result.error or '未识别到文字')
            return f'{engine.display_name} → {result.text.strip()[:30]!r}'

        check('本地 OCR 识别', run_ocr)

        def parse_name():
            from models import NamingConfig
            from ocr.suggester import suggest_for_text
            suggestion = suggest_for_text('姓名：张三\n性别：男',
                                          NamingConfig(fields=['姓名']))
            if suggestion.name != '张三':
                raise RuntimeError(f'命名结果异常：{suggestion.name!r}')
            return f'识别为 {suggestion.name}（{suggestion.method}）'

        check('字段提取与命名', parse_name)

        def copy_safety():
            from utils import file_utils
            src = os.path.join(temp_dir, 'src.bin')
            dst = os.path.join(temp_dir, 'dst.bin')
            with open(src, 'wb') as handle:
                handle.write(b'SAFETY-TEST')
            file_utils.copy_no_overwrite(src, dst)
            try:
                file_utils.copy_no_overwrite(src, dst)
            except FileExistsError:
                return '已存在时拒绝覆盖 ✔'
            raise RuntimeError('安全机制失效：竟然允许覆盖！')

        check('防覆盖机制', copy_safety)

        def engine_status():
            from ocr.engine import available_engine_keys
            keys = available_engine_keys()
            if not keys:
                raise RuntimeError('没有可用的 OCR 引擎')
            return ','.join(keys)

        check('可用 OCR 引擎', engine_status)

    except Exception as exc:
        lines.append(f'[FAIL] 自检过程异常：{exc}')
        problems.append('自检过程')
    finally:
        if temp_dir:
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    lines.append('-' * 60)
    lines.append('结果：' + ('全部通过 ✔' if not problems
                             else f'{len(problems)} 项失败：{", ".join(problems)}'))
    report = '\n'.join(lines)

    try:
        target_dir = os.path.join(os.environ.get('LOCALAPPDATA') or BASE_DIR, 'PicHarvest')
        os.makedirs(target_dir, exist_ok=True)
        report_path = os.path.join(target_dir, 'selftest_report.txt')
        with open(report_path, 'w', encoding='utf-8') as handle:
            handle.write(report)
    except Exception:
        report_path = '(无法写入报告文件)'

    try:
        print(report)
        print(f'\n报告已保存：{report_path}')
    except Exception:
        pass

    return 1 if problems else 0


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    prepare_environment()
    argv = argv if argv is not None else sys.argv

    if '--selftest' in argv:
        return run_selftest()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print('缺少 PySide6，请先执行：pip install -r requirements.txt', file=sys.stderr)
        return 2

    from utils.logger import get_logger, setup_app_logging
    setup_app_logging()
    logger = get_logger('main')
    logger.info('=' * 60)
    logger.info('程序启动')

    install_excepthook()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName('PicHarvest')
    app.setApplicationDisplayName('图片智能提取与 OCR 命名工具')
    app.setOrganizationName('PicHarvest')

    from ui.style import apply_style
    apply_style(app)

    from ui.main_window import MainWindow
    window = MainWindow()
    window.show()

    # 首次运行提示 OCR 环境
    try:
        from ocr.engine import available_engine_keys
        if not available_engine_keys():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                window, '提示：尚未安装 OCR 引擎',
                '当前环境没有检测到可用的本地 OCR 引擎，程序仍可用于\n'
                '"按路径规则提取图片 + 用来源文件夹命名"。\n\n'
                '如需 OCR 自动命名，请在 Python 3.11/3.12 环境中执行：\n'
                '    pip install rapidocr-onnxruntime     （轻量，约 100MB）\n'
                '    pip install paddlepaddle paddleocr   （精度更高，体积较大）\n\n'
                '所有 OCR 均在本机运行，图片不会上传到任何服务器。')
    except Exception:
        pass

    try:
        return app.exec()
    finally:
        try:
            from ocr.engine import reset_engines
            reset_engines()
        except Exception:
            pass
        logger.info('程序退出')


if __name__ == '__main__':
    # PyInstaller 打包后需要，防止子进程重复启动界面
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass
    raise SystemExit(main())

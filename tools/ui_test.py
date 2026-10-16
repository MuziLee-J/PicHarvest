"""
无头 GUI 冒烟测试（离屏渲染，自动点完全流程）。

用法：
    python tools/ui_test.py            # 扫描 → OCR → 执行 → 撤销
    python tools/ui_test.py --no-ocr

只用于开发自检：验证界面控件、信号连接、线程协作是否正常。
"""

from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def pump(app, seconds: float = 0.05) -> None:
    """驱动事件循环。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, predicate, timeout: float = 300.0, what: str = '任务') -> bool:
    """等待条件成立。"""
    start = time.time()
    while time.time() - start < timeout:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    raise TimeoutError(f'等待{what}超时（{timeout} 秒）')


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    # 屏蔽所有弹窗，避免测试阻塞
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.about = staticmethod(lambda *a, **k: None)

    app = QApplication(sys.argv)

    from ui.style import apply_style
    apply_style(app)
    from ui.main_window import MainWindow

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_root = os.path.join(base, '_testdata')
    output = os.path.join(data_root, '_ui_output')
    if os.path.isdir(output):
        shutil.rmtree(output, ignore_errors=True)

    from tools.make_test_data import build
    source_root = build(data_root, with_images=True)

    window = MainWindow()
    window.show()
    window._confirm_execute = lambda text: True     # 自动确认执行
    pump(app, 0.2)
    print('[1] 主窗口创建成功')

    # ---- 填表 ----
    page = window.settings_page
    page.root_edit.setText(source_root)
    page.output_edit.setText(output)
    page._read_dirs()
    pump(app, 0.2)
    page.level1_combo.setCurrentIndex(0)            # 所有文件夹
    page._restore_combo(page.level2_combo, '1440')
    page.file_combo.setCurrentText('SAMPLE_NA.jpg')
    pump(app, 0.1)
    print(f'[2] 目录读取完成：第一层候选 {page.level1_combo.count()} 个，'
          f'第二层候选 {page.level2_combo.count()} 个，'
          f'文件名候选 {page.file_combo.count()} 个')
    print(f'    规则预览：{page._current_template()}')

    # ---- 扫描 ----
    window.on_scan()
    wait_for(app, lambda: window.worker is None, what='扫描')
    pump(app, 0.2)
    assert window.stack.currentIndex() == 1, '应跳转到扫描结果页'
    count = window.scan_page.model.rowCount()
    print(f'[3] 扫描完成：表格 {count} 行，界面显示「{window.scan_page.count_label.text()}」')
    assert count == 6, f'期望命中 6 个文件，实际 {count}'

    # 勾选交互
    window.scan_page.select_none()
    pump(app, 0.1)
    window.scan_page.select_all()
    pump(app, 0.1)
    print(f'[4] 勾选/取消勾选正常：{window.scan_page.count_label.text()}')

    # 缩略图
    window.scan_page.thumb_check.setChecked(True)
    pump(app, 1.0)
    thumbs = sum(1 for p in window.scan_page.model.thumbnails._cache)
    print(f'[5] 缩略图加载：{thumbs} 张（后台线程）')

    # ---- OCR ----
    if '--no-ocr' not in sys.argv:
        window.on_ocr()
        wait_for(app, lambda: window.worker is None, timeout=600, what='OCR')
        pump(app, 0.3)
        assert window.stack.currentIndex() == 2
        names = [(r.index, r.extracted_name, r.status)
                 for r in window.manager.records]
        print(f'[6] OCR 完成：{window.ocr_page.count_label.text()}')
        for index, name, status in names:
            print(f'      #{index} {name or "(无)"} -> {status}')
        ready = [r for r in window.manager.records if r.status == 'ready']
        assert len(ready) >= 3, '至少应有 3 条识别成功'
    else:
        for record in window.manager.records:
            window.manager.set_manual_name(record, '测试名')
        window.ocr_page.refresh_records()
        print('[6] 跳过 OCR')

    # ---- 执行 ----
    window.on_execute(include_unreviewed=True)
    wait_for(app, lambda: window.worker is None, timeout=600, what='执行')
    pump(app, 0.3)
    assert window.stack.currentIndex() == 3
    summary = window.manager.summary()
    print(f'[7] 执行完成：成功 {summary.success}，失败 {summary.failed}，'
          f'跳过 {summary.skipped}，重名 {summary.conflicts}')
    files = [f for f in sorted(os.listdir(output)) if f != '_reports']
    print('    输出目录：' + ', '.join(files))
    assert summary.success == 6, '应全部复制成功'

    reports_dir = os.path.join(output, '_reports')
    reports = os.listdir(reports_dir) if os.path.isdir(reports_dir) else []
    print(f'[8] 报告文件：{reports}')

    # ---- 撤销 ----
    window.on_undo()
    wait_for(app, lambda: window.worker is None, timeout=120, what='撤销')
    pump(app, 0.2)
    remaining = [f for f in os.listdir(output) if f != '_reports']
    print(f'[9] 撤销后输出目录剩余：{remaining}')
    assert not remaining, '撤销后不应剩下文件'

    # ---- 历史 ----
    from ui.main_window import HistoryDialog
    dialog = HistoryDialog(window.database, window)
    rows = dialog.table.rowCount()
    print(f'[10] 任务历史：{rows} 条记录')
    dialog.close()

    window.close()
    pump(app, 0.2)
    print('\n✅ 界面冒烟测试全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

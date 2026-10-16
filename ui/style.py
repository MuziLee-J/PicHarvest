"""界面样式与字体（集中管理，方便统一换肤）。"""

from __future__ import annotations

#: 主色
PRIMARY = '#2f6fb0'
PRIMARY_DARK = '#245a91'
SUCCESS = '#1f8a4c'
WARNING = '#c47f0a'
DANGER = '#c0392b'
MUTED = '#6b7280'
BORDER = '#d6dbe1'
BG_SOFT = '#f6f8fa'

STYLESHEET = f"""
QWidget {{
    font-size: 13px;
    color: #1f2328;
}}
QMainWindow, QDialog {{
    background: #ffffff;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    background: #ffffff;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {PRIMARY_DARK};
}}
QLabel#hint {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#title {{
    font-size: 16px;
    font-weight: bold;
    color: {PRIMARY_DARK};
}}
QLabel#counter {{
    font-size: 13px;
    font-weight: bold;
}}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QListWidget, QTableView {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    background: #ffffff;
    selection-background-color: {PRIMARY};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {PRIMARY};
}}
QLineEdit:disabled, QComboBox:disabled {{
    background: {BG_SOFT};
    color: {MUTED};
}}
QPushButton {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 14px;
    background: #ffffff;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {BG_SOFT};
    border-color: {PRIMARY};
}}
QPushButton:disabled {{
    color: #a5adb6;
    background: #f2f4f6;
    border-color: #e3e7eb;
}}
QPushButton#primary {{
    background: {PRIMARY};
    color: white;
    border: none;
    font-weight: bold;
}}
QPushButton#primary:hover {{ background: {PRIMARY_DARK}; }}
QPushButton#primary:disabled {{ background: #a9c2dc; color: #eef3f8; }}
QPushButton#danger {{
    background: {DANGER};
    color: white;
    border: none;
    font-weight: bold;
}}
QPushButton#danger:hover {{ background: #a5301f; }}
QPushButton#ghost {{
    background: transparent;
    border: none;
    color: {PRIMARY_DARK};
    text-decoration: underline;
    padding: 2px 6px;
}}
QHeaderView::section {{
    background: {BG_SOFT};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 6px;
    font-weight: bold;
}}
QTableView {{
    gridline-color: #eceff2;
    selection-background-color: #d9e7f5;
    selection-color: #1f2328;
}}
QTableView::item:selected {{ background: #d9e7f5; color: #1f2328; }}
QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    height: 20px;
    background: {BG_SOFT};
}}
QProgressBar::chunk {{
    background: {PRIMARY};
    border-radius: 3px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER}; }}
QTabBar::tab {{
    padding: 6px 16px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    background: {BG_SOFT};
}}
QTabBar::tab:selected {{ background: #ffffff; color: {PRIMARY_DARK}; font-weight: bold; }}
QStatusBar {{ color: {MUTED}; }}
QSplitter::handle {{ background: {BG_SOFT}; width: 2px; }}
"""

#: 状态颜色
STATUS_COLORS = {
    'pending': MUTED,
    'ocr_done': PRIMARY,
    'ready': SUCCESS,
    'needs_review': WARNING,
    'skipped': MUTED,
    'success': SUCCESS,
    'failed': DANGER,
    'canceled': MUTED,
}


def apply_style(app) -> None:
    """应用全局样式与字体。"""
    from PySide6.QtGui import QFont

    app.setStyleSheet(STYLESHEET)
    for family in ('Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC',
                   'Noto Sans CJK SC', 'SimHei'):
        font = QFont(family, 9)
        if font.exactMatch() or family.startswith('Microsoft YaHei'):
            app.setFont(QFont(family, 9))
            break

"""
第二步：扫描结果。

展示"准备从哪些完整路径提取哪些图片"，支持勾选、全选、筛选、排序、预览。
本页所有操作都只改内存中的记录状态，不触碰任何文件。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSplitter, QTableView, QVBoxLayout,
                               QWidget)

from models import ImageRecord, ScanResult, Status
from ui.widgets import (Column, ImagePreview, RecordFilterProxy, RecordTableModel,
                        SCAN_COLUMNS, configure_table_view, make_hint,
                        selected_record)
from utils import file_utils


class ScanPage(QWidget):
    """扫描结果页。"""

    back_requested = Signal()
    ocr_requested = Signal()
    rescan_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: list[ImageRecord] = []
        self.manager = None
        self._build()

    # ================================================================ 界面

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 12)

        header = QHBoxLayout()
        title = QLabel('第二步 · 扫描结果')
        title.setObjectName('title')
        header.addWidget(title)
        header.addStretch(1)
        self.count_label = QLabel('尚未扫描')
        self.count_label.setObjectName('counter')
        header.addWidget(self.count_label)
        layout.addLayout(header)

        self.summary_label = QLabel('')
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            'background:#f2f7fc; border:1px solid #cfe0f0; border-radius:4px; '
            'padding:8px; color:#245a91;')
        layout.addWidget(self.summary_label)

        layout.addWidget(make_hint(
            '提示：下面的清单就是"即将被复制的文件"。请确认路径无误后再点击"开始 OCR"。'
            '本步骤不会修改任何文件。'))

        # 工具条
        tools = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('筛选：输入文件夹名 / 文件名 / 路径关键字')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search)
        tools.addWidget(self.search_edit, 1)

        self.status_combo = QComboBox()
        self.status_combo.addItem('全部状态', '')
        self.status_combo.addItem('只看需人工确认', RecordFilterProxy.STATUS_REVIEW)
        self.status_combo.addItem('只看已勾选（待处理）', Status.PENDING)
        self.status_combo.currentIndexChanged.connect(self._on_filter_status)
        tools.addWidget(self.status_combo)

        self.thumb_check = QCheckBox('显示缩略图')
        self.thumb_check.toggled.connect(self._on_thumb_toggled)
        tools.addWidget(self.thumb_check)

        for text, slot in (('全选', self.select_all), ('取消全选', self.select_none),
                           ('反选', self.invert_selection)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            tools.addWidget(button)
        layout.addLayout(tools)

        # 表格 + 预览
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableView()
        self.model = RecordTableModel(SCAN_COLUMNS)
        self.model.select_toggled.connect(self._on_select_toggled)
        self.proxy = RecordFilterProxy(self.model)
        configure_table_view(self.table, self.model, self.proxy.proxy, row_height=30)
        self.table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = ImagePreview(title='图片预览')
        right_layout.addWidget(self.preview, 1)
        self.detail_label = QLabel('')
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName('hint')
        right_layout.addWidget(self.detail_label)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 420])
        layout.addWidget(splitter, 1)

        # 底部按钮
        bottom = QHBoxLayout()
        back_button = QPushButton('← 返回设置')
        back_button.clicked.connect(self.back_requested.emit)
        bottom.addWidget(back_button)
        rescan_button = QPushButton('重新扫描')
        rescan_button.clicked.connect(self.rescan_requested.emit)
        bottom.addWidget(rescan_button)
        bottom.addStretch(1)
        self.ocr_button = QPushButton('开始 OCR →')
        self.ocr_button.setObjectName('primary')
        self.ocr_button.setMinimumWidth(150)
        self.ocr_button.clicked.connect(self._on_ocr_clicked)
        bottom.addWidget(self.ocr_button)
        layout.addLayout(bottom)

    # ================================================================ 数据

    def set_manager(self, manager) -> None:
        self.manager = manager

    def show_result(self, result: ScanResult, template: str, output_dir: str) -> None:
        """展示扫描结果。"""
        self.records = result.files
        self.model.set_records(self.records)
        self.preview.clear()
        self.count_label.setText(f'找到 {result.count} 个文件')

        lines = [
            f'<b>根目录：</b>{result.root_dir}',
            f'<b>匹配规则：</b>{template}',
            f'<b>输出目录：</b>{output_dir or "（未设置）"}',
            f'<b>命中文件：</b>{result.count} 个　'
            f'<b>访问目录：</b>{result.scanned_dirs} 个　'
            f'<b>耗时：</b>{result.elapsed:.2f} 秒',
        ]
        if result.errors:
            lines.append(f'<span style="color:#c47f0a">扫描中有 {len(result.errors)} 处警告'
                         f'（通常是权限或临时文件），已跳过，不影响结果。</span>')
        if result.count == 0:
            lines.append('<span style="color:#c0392b">没有找到任何文件。请检查：'
                         '1) 第二层目录名是否正确；2) 图片名是否正确；'
                         '3) 根目录是否选错。</span>')
        self.summary_label.setText('<br>'.join(lines))
        self.ocr_button.setEnabled(result.count > 0)

    # ================================================================ 交互

    def _on_search(self, text: str) -> None:
        self.proxy.set_text(text)

    def _on_filter_status(self) -> None:
        self.proxy.set_status(self.status_combo.currentData() or '')

    def _on_thumb_toggled(self, checked: bool) -> None:
        if self.model.thumbnails is None and checked:
            from ui.widgets import ThumbnailProvider
            self.model.thumbnails = ThumbnailProvider(size=64)
            self.model.thumbnails.updated.connect(self._on_thumb_ready)
        self.table.setColumnHidden(2, not checked)
        if checked:
            self.model.refresh()

    def _on_thumb_ready(self, path: str) -> None:
        row = self.model.row_of_path(path)
        if row >= 0:
            self.model.refresh(row)

    def _on_select_toggled(self, payload) -> None:
        record, checked = payload
        if self.manager is not None:
            if checked:
                self.manager.unskip_record(record)
            else:
                self.manager.skip_record(record, '用户在扫描结果中取消勾选')
        try:
            row = self.model.records.index(record)
            self.model.refresh(row)
        except ValueError:
            self.model.refresh()
        self._update_selection_count()

    def _update_selection_count(self) -> None:
        total = len(self.records)
        selected = sum(1 for r in self.records if r.status != Status.SKIPPED)
        if total:
            self.count_label.setText(f'共 {total} 个文件，已勾选 {selected} 个')

    def select_all(self) -> None:
        self._bulk_select(lambda record: True)

    def select_none(self) -> None:
        self._bulk_select(lambda record: False)

    def invert_selection(self) -> None:
        self._bulk_select(lambda record: record.status == Status.SKIPPED)

    def _bulk_select(self, predicate) -> None:
        for record in self.records:
            want_selected = predicate(record)
            if want_selected and record.status == Status.SKIPPED:
                if self.manager:
                    self.manager.unskip_record(record)
            elif not want_selected and record.status != Status.SKIPPED:
                if self.manager:
                    self.manager.skip_record(record, '用户取消勾选')
        self.model.refresh()
        self._update_selection_count()

    def _on_row_changed(self, current, _previous) -> None:
        record = selected_record(self.table, self.model, self.proxy.proxy)
        if record is None:
            return
        size_text = file_utils.human_size(record.size)
        info = (f'{record.source_path}\n文件大小：{size_text}\n'
                f'来源文件夹：{record.source_folder}\n状态：{Status.label(record.status)}')
        self.preview.show_path(record.source_path, info)
        self.detail_label.setText(f'来源文件夹：{record.source_folder}')

    def _on_ocr_clicked(self) -> None:
        selected = [r for r in self.records if r.status != Status.SKIPPED]
        if not selected:
            QMessageBox.information(self, '没有可处理的文件',
                                    '请至少勾选一个文件后再开始 OCR。')
            return
        self.ocr_requested.emit()

    def set_busy(self, busy: bool) -> None:
        self.ocr_button.setEnabled(not busy and len(self.records) > 0)
        self.ocr_button.setText('正在 OCR…' if busy else '开始 OCR →')

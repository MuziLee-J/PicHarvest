"""
第三步：OCR 与命名预览。

对每张图片展示：原图 → OCR 文本 → 识别字段 → 建议文件名 → 最终文件名。
用户可以逐条采用 / 修改 / 重新 OCR / 跳过，也可以一键采用全部建议名。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
                               QTableView, QVBoxLayout, QWidget)

from models import ExtractMethod, ImageRecord, Status
from ui.widgets import (ImagePreview, OCR_COLUMNS, RecordFilterProxy,
                        RecordTableModel, configure_table_view, make_hint,
                        selected_record)
from utils import file_utils


class OcrPage(QWidget):
    """OCR 与命名预览页。"""

    back_requested = Signal()
    execute_requested = Signal(bool)          # include_unreviewed
    reocr_requested = Signal(object)          # ImageRecord
    accept_all_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = None
        self._current: ImageRecord | None = None
        self._loading = False
        self._build()

    # ================================================================ 界面

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 12)

        header = QHBoxLayout()
        title = QLabel('第三步 · OCR 与命名预览')
        title.setObjectName('title')
        header.addWidget(title)
        header.addStretch(1)
        self.count_label = QLabel('')
        self.count_label.setObjectName('counter')
        header.addWidget(self.count_label)
        layout.addLayout(header)

        layout.addWidget(make_hint(
            '命名优先级：① 指定字段 → ② 本地 AI 判断 → ③ 人工确认。'
            '标记为"需人工确认"的文件，只有在您确认（或勾选下方的"包含未确认项"）后才会被复制。'))

        # OCR 进度（识别过程中显示，结束后隐藏）
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat('%p%  (%v / %m)')
        progress_row.addWidget(self.progress, 1)
        self.progress_label = QLabel('')
        self.progress_label.setObjectName('hint')
        self.progress_label.setMinimumWidth(280)
        progress_row.addWidget(self.progress_label)
        layout.addLayout(progress_row)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)

        # 工具条
        tools = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('筛选：文件夹 / OCR 文本 / 文件名')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda t: self.proxy.set_text(t))
        tools.addWidget(self.search_edit, 1)

        self.filter_combo = QComboBox()
        self.filter_combo.addItem('全部', '')
        self.filter_combo.addItem('只看需人工确认', RecordFilterProxy.STATUS_REVIEW)
        self.filter_combo.addItem('只看识别成功', Status.READY)
        self.filter_combo.addItem('只看已跳过', Status.SKIPPED)
        self.filter_combo.currentIndexChanged.connect(
            lambda: self.proxy.set_status(self.filter_combo.currentData() or ''))
        tools.addWidget(self.filter_combo)

        self.accept_all_button = QPushButton('全部采用建议名')
        self.accept_all_button.clicked.connect(self._on_accept_all)
        tools.addWidget(self.accept_all_button)

        self.retry_failed_button = QPushButton('重试失败项')
        self.retry_failed_button.clicked.connect(self._on_retry_failed)
        tools.addWidget(self.retry_failed_button)
        layout.addLayout(tools)

        # 表格 + 详情
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableView()
        self.model = RecordTableModel(OCR_COLUMNS)
        self.model.select_toggled.connect(self._on_select_toggled)
        self.proxy = RecordFilterProxy(self.model)
        configure_table_view(self.table, self.model, self.proxy.proxy, row_height=28)
        self.table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([840, 460])
        layout.addWidget(splitter, 1)

        # 底部
        bottom = QHBoxLayout()
        back_button = QPushButton('← 返回扫描结果')
        back_button.clicked.connect(self.back_requested.emit)
        bottom.addWidget(back_button)

        self.include_check = QCheckBox('包含"需人工确认"的文件（使用其建议名复制）')
        self.include_check.setToolTip(
            '默认不勾选：未确认的文件不会被复制，会记入报告的"跳过"。')
        bottom.addWidget(self.include_check)
        bottom.addStretch(1)

        self.execute_button = QPushButton('确认并执行复制 →')
        self.execute_button.setObjectName('primary')
        self.execute_button.setMinimumWidth(180)
        self.execute_button.clicked.connect(self._on_execute_clicked)
        bottom.addWidget(self.execute_button)
        layout.addLayout(bottom)

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.preview = ImagePreview(title='原图预览')
        layout.addWidget(self.preview, 2)

        group = QGroupBox('识别与命名')
        form = QVBoxLayout(group)

        form.addWidget(QLabel('OCR 结果：'))
        self.ocr_text_edit = QPlainTextEdit()
        self.ocr_text_edit.setReadOnly(True)
        self.ocr_text_edit.setMaximumHeight(140)
        self.ocr_text_edit.setPlaceholderText('（未识别到文字）')
        form.addWidget(self.ocr_text_edit)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel('识别名称：'))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('留空表示无法识别')
        self.name_edit.editingFinished.connect(self._on_name_edited)
        row1.addWidget(self.name_edit, 1)
        form.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel('最终文件名：'))
        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText('可直接修改（系统会自动清洗非法字符）')
        self.filename_edit.editingFinished.connect(self._on_filename_edited)
        row2.addWidget(self.filename_edit, 1)
        form.addLayout(row2)

        self.suggest_label = QLabel('')
        self.suggest_label.setObjectName('hint')
        self.suggest_label.setWordWrap(True)
        form.addWidget(self.suggest_label)

        buttons = QHBoxLayout()
        self.accept_button = QPushButton('采用')
        self.accept_button.clicked.connect(self._on_accept)
        buttons.addWidget(self.accept_button)

        self.reocr_button = QPushButton('重新 OCR')
        self.reocr_button.clicked.connect(self._on_reocr)
        buttons.addWidget(self.reocr_button)

        self.skip_button = QPushButton('跳过')
        self.skip_button.clicked.connect(lambda: self._on_skip(True))
        buttons.addWidget(self.skip_button)

        self.restore_button = QPushButton('恢复')
        self.restore_button.clicked.connect(lambda: self._on_skip(False))
        buttons.addWidget(self.restore_button)

        self.open_button = QPushButton('打开图片')
        self.open_button.clicked.connect(self._on_open_image)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        form.addLayout(buttons)

        layout.addWidget(group, 3)
        return panel

    # ================================================================ 数据

    def set_manager(self, manager) -> None:
        self.manager = manager

    def refresh_records(self) -> None:
        """重新载入记录（OCR 完成或批量操作后调用）。"""
        if self.manager is None:
            return
        self.model.set_records(self.manager.records)
        self._update_counts()
        current = self._current
        if current is not None and current in self.manager.records:
            self._show_record(current)
        elif self.manager.records:
            self.table.selectRow(0)

    def update_record(self, record: ImageRecord) -> None:
        """单条记录刷新。"""
        try:
            row = self.model.records.index(record)
            self.model.refresh(row)
        except ValueError:
            self.model.refresh()
        self._update_counts()

    def update_record_by_index(self, index: int) -> None:
        """按序号刷新（OCR 过程中批量刷新用，避免整表重绘）。"""
        for row, record in enumerate(self.model.records):
            if record.index == index:
                self.model.refresh(row)
                break
        self._update_counts()

    def _update_counts(self) -> None:
        records = self.model.records
        total = len(records)
        ready = sum(1 for r in records if r.status == Status.READY)
        review = sum(1 for r in records if r.status == Status.NEEDS_REVIEW)
        skipped = sum(1 for r in records if r.status == Status.SKIPPED)
        self.count_label.setText(
            f'共 {total} 个　就绪 {ready}　需确认 {review}　已跳过 {skipped}')

    # ================================================================ 交互

    def _on_row_changed(self, current, _previous) -> None:
        record = selected_record(self.table, self.model, self.proxy.proxy)
        if record is not None:
            self._show_record(record)

    def _show_record(self, record: ImageRecord) -> None:
        self._loading = True
        try:
            self._current = record
            self.preview.show_path(record.source_path, '')
            self.ocr_text_edit.setPlainText(record.ocr_text or '')
            self.name_edit.setText(record.extracted_name or '')
            self.filename_edit.setText(record.final_filename or
                                       record.suggested_filename or '')

            method = ExtractMethod.LABELS.get(record.extract_method,
                                              record.extract_method or '未识别')
            lines = [
                f'来源文件夹：{record.source_folder}　方式：{method}　'
                f'状态：{Status.label(record.status)}',
                f'系统建议：{record.extracted_name or "（无）"}',
            ]
            if record.error:
                lines.append(f'<span style="color:#c47f0a">提示：{record.error}</span>')
            if record.conflict_note:
                lines.append(f'<span style="color:#c47f0a">{record.conflict_note}</span>')
            self.suggest_label.setText('<br>'.join(lines))
        finally:
            self._loading = False

    def _on_select_toggled(self, payload) -> None:
        record, checked = payload
        if self.manager is None:
            return
        if checked:
            self.manager.unskip_record(record)
        else:
            self.manager.skip_record(record, '用户在命名页跳过')
        self.update_record(record)

    def _on_name_edited(self) -> None:
        if self._loading or self.manager is None or self._current is None:
            return
        text = self.name_edit.text().strip()
        if text == (self._current.extracted_name or ''):
            return
        self.manager.set_manual_name(self._current, text)
        self.update_record(self._current)
        self._show_record(self._current)

    def _on_filename_edited(self) -> None:
        if self._loading or self.manager is None or self._current is None:
            return
        text = self.filename_edit.text().strip()
        if text == (self._current.final_filename or ''):
            return
        self.manager.set_final_filename(self._current, text)
        self.update_record(self._current)
        self._show_record(self._current)

    def _on_accept(self) -> None:
        if self.manager is None or self._current is None:
            return
        name = self.name_edit.text().strip() or self._current.extracted_name
        self.manager.set_manual_name(self._current, name)
        self.update_record(self._current)
        self._show_record(self._current)
        # 自动跳到下一条需确认的记录
        self._goto_next_review()

    def _goto_next_review(self) -> None:
        records = self.model.records
        start = records.index(self._current) + 1 if self._current in records else 0
        for offset in range(len(records)):
            record = records[(start + offset) % len(records)]
            if record.status == Status.NEEDS_REVIEW:
                proxy_index = self.proxy.proxy.mapFromSource(self.model.index(
                    records.index(record), 0))
                if proxy_index.isValid():
                    self.table.selectRow(proxy_index.row())
                return

    def _on_reocr(self) -> None:
        if self._current is None:
            return
        self.reocr_requested.emit(self._current)

    def _on_skip(self, skip: bool) -> None:
        if self.manager is None or self._current is None:
            return
        if skip:
            self.manager.skip_record(self._current, '用户跳过')
        else:
            self.manager.unskip_record(self._current)
        self.update_record(self._current)
        self._show_record(self._current)

    def _on_open_image(self) -> None:
        if self._current is not None:
            file_utils.open_in_explorer(self._current.source_path)

    def _on_accept_all(self) -> None:
        """批量采用建议名。"""
        if self.manager is None:
            return
        pending = [r for r in self.manager.records
                   if r.status == Status.NEEDS_REVIEW and r.extracted_name]
        if not pending:
            QMessageBox.information(self, '无需处理',
                                    '当前没有"需人工确认"的记录（或它们没有建议名）。')
            return
        answer = QMessageBox.question(
            self, '批量采用建议名',
            f'将对 {len(pending)} 条"需人工确认"的记录采用系统建议名。\n\n'
            '这些建议来自本地 AI 判断，可能不完全准确，建议您仍逐条抽查。\n\n继续吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        for record in pending:
            self.manager.set_manual_name(record, record.extracted_name)
        self.model.refresh()
        self._update_counts()

    def _on_retry_failed(self) -> None:
        if self.manager is None:
            return
        targets = [r for r in self.manager.records
                   if r.status != Status.SKIPPED and not r.extracted_name]
        if not targets:
            QMessageBox.information(self, '无需重试', '没有识别失败的记录。')
            return
        self.reocr_requested.emit(targets[0])
        QMessageBox.information(
            self, '重试提示',
            f'已开始重新识别第 {targets[0].index} 条。\n'
            f'如需批量重试，请在主界面点击"开始 OCR"并选择"只处理未识别的文件"。')

    def _on_execute_clicked(self) -> None:
        if self.manager is None:
            return
        include_unreviewed = self.include_check.isChecked()
        review = [r for r in self.manager.records if r.status == Status.NEEDS_REVIEW]
        if review and not include_unreviewed:
            answer = QMessageBox.question(
                self, '有未确认的文件',
                f'还有 {len(review)} 个文件处于"需人工确认"状态。\n\n'
                f'• 点击"是"：本次不复制它们（会记入报告的"跳过"）\n'
                f'• 点击"否"：返回继续确认\n\n'
                f'也可以勾选"包含未经确认的文件"后再执行。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.execute_requested.emit(include_unreviewed)

    def set_busy(self, busy: bool) -> None:
        for widget in (self.execute_button, self.accept_all_button,
                       self.retry_failed_button, self.reocr_button):
            widget.setEnabled(not busy)
        self.execute_button.setText('正在处理…' if busy else '确认并执行复制 →')

    # ================================================================ 进度

    def start_progress(self, total: int, message: str = '正在识别…') -> None:
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.progress_label.setText(message)

    def set_progress(self, current: int, total: int, message: str = '') -> None:
        if total:
            self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.progress_label.setText(message or f'{current} / {total}')

    def end_progress(self, message: str = '') -> None:
        self.progress_label.setText(message)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)

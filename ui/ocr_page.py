"""
第三步：OCR 与命名预览。

对每张图片展示：原图 → OCR 文本 → 识别字段 → 建议文件名 → 最终文件名。
用户可以逐条采用 / 修改 / 重新 OCR / 跳过，也可以一键采用全部建议名。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSplitter, QTableView,
                               QVBoxLayout, QWidget)

from models import ExtractMethod, ImageRecord, Status
from ui.widgets import (ElidedLabel, ImagePreview, OCR_COLUMNS,
                        RecordFilterProxy, RecordTableModel,
                        configure_table_view, make_hint, selected_record)
from utils import file_utils


class OcrPage(QWidget):
    """OCR 与命名预览页。"""

    back_requested = Signal()
    execute_requested = Signal(bool)          # include_unreviewed
    reocr_requested = Signal(object)          # ImageRecord
    accept_all_requested = Signal()
    force_stop_requested = Signal()
    output_dir_changed = Signal(str)      # OCR 后改输出目录（只需重算文件名）

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

        # 降级提示：子进程 OCR 起不来时会退回进程内模式，行为不一样（P1-4）
        self.degrade_banner = QLabel('')
        self.degrade_banner.setWordWrap(True)
        self.degrade_banner.setStyleSheet(
            'color:#7a4b00; background:#fff8e6; border:1px solid #f0d48a;'
            'border-radius:6px; padding:8px 10px;')
        self.degrade_banner.setVisible(False)
        layout.addWidget(self.degrade_banner)

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

        self.force_stop_button = QPushButton('强制停止')
        self.force_stop_button.setObjectName('danger')
        self.force_stop_button.setToolTip(
            '立即结束正在进行的 OCR：后台识别进程会被直接终止。\n'
            '已经识别完的文件保留结果。')
        self.force_stop_button.clicked.connect(self.force_stop_requested.emit)
        progress_row.addWidget(self.force_stop_button)

        layout.addLayout(progress_row)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.force_stop_button.setVisible(False)

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
        # 列宽固定（见 widgets.OCR_COLUMNS），窗口不够宽时用横向滚动条，
        # 不再让中间列被 Stretch 压成一条窄缝。
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setMinimumSectionSize(48)
        splitter.addWidget(self.table)

        detail = self._build_detail()
        detail.setMinimumWidth(360)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([880, 380])
        layout.addWidget(splitter, 1)

        # 输出目录：跑完 OCR 才发现选错位置时，在这里直接改，不用重来
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel('输出目录：'))
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText('图片将复制到这里，可随时修改')
        self.output_dir_edit.setToolTip(
            '改这里只会重新计算目标文件名与路径，已完成的 OCR 结果全部保留，'
            '不需要重新识别。')
        self.output_dir_edit.editingFinished.connect(self._on_output_dir_edited)
        dir_row.addWidget(self.output_dir_edit, 1)
        self.output_dir_button = QPushButton('浏览…')
        self.output_dir_button.clicked.connect(self._pick_output_dir)
        dir_row.addWidget(self.output_dir_button)
        layout.addLayout(dir_row)

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
        layout.addWidget(self.preview, 1)

        group = QGroupBox('识别与命名')
        form = QGridLayout(group)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setColumnMinimumWidth(0, 78)
        form.setColumnStretch(1, 1)

        form.addWidget(QLabel('OCR 结果：'), 0, 0, 1, 2)
        self.ocr_text_edit = QPlainTextEdit()
        self.ocr_text_edit.setReadOnly(True)
        self.ocr_text_edit.setMinimumHeight(56)
        self.ocr_text_edit.setMaximumHeight(110)
        self.ocr_text_edit.setPlaceholderText('（未识别到文字）')
        form.addWidget(self.ocr_text_edit, 1, 0, 1, 2)

        name_label = QLabel('识别名称：')
        name_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        form.addWidget(name_label, 2, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('留空表示无法识别')
        self.name_edit.editingFinished.connect(self._on_name_edited)
        form.addWidget(self.name_edit, 2, 1)

        filename_label = QLabel('最终文件名：')
        filename_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        form.addWidget(filename_label, 3, 0)
        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText('可直接修改（系统会自动清洗非法字符）')
        self.filename_edit.editingFinished.connect(self._on_filename_edited)
        form.addWidget(self.filename_edit, 3, 1)

        self.suggest_label = QLabel('')
        self.suggest_label.setObjectName('hint')
        self.suggest_label.setWordWrap(True)
        form.addWidget(self.suggest_label, 4, 0, 1, 2)

        dest_caption = QLabel('将复制到：')
        dest_caption.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        form.addWidget(dest_caption, 5, 0)
        # 目标路径往往很长，用单行省略标签：不会换行把面板撑高，
        # 完整路径放在 tooltip 里，需要时悬停或选中复制即可。
        self.dest_edit = ElidedLabel('')
        self.dest_edit.setObjectName('hint')
        form.addWidget(self.dest_edit, 5, 1)

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
        form.addLayout(buttons, 6, 0, 1, 2)

        # 表单一类固定高度，多余空间交给上面的预览图，避免行距被拉开。
        layout.addWidget(group, 0)

        # 窗口很矮时（例如 1024x700）右侧内容放不下，用滚动区兜住，
        # 否则布局会把控件压到互相重叠；高度够时滚动条自动隐藏。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        return scroll

    # ================================================================ 数据

    def set_manager(self, manager) -> None:
        self.manager = manager

    # ---------------------------------------------------------------- 降级提示

    def show_degrade(self, reason: str) -> None:
        """显示「已降级为进程内 OCR」的常驻提示（P1-4）。"""
        text = (reason or '').strip() or 'OCR 子进程不可用'
        self.degrade_banner.setText(
            f'⚠ {text}　本次改用进程内 OCR：单张识别超时不会中断，'
            '"强制停止"也不可用。关闭窗口会等到当前文件处理完。')
        self.degrade_banner.setVisible(True)

    def clear_degrade(self) -> None:
        self.degrade_banner.clear()
        self.degrade_banner.setVisible(False)

    # ---------------------------------------------------------------- 输出目录

    def set_output_dir(self, path: str) -> None:
        """外部（设置页 / 主窗口）改了目录后同步输入框，不触发信号。"""
        if (self.output_dir_edit.text().strip() or '') == (path or '').strip():
            return
        self.output_dir_edit.blockSignals(True)
        self.output_dir_edit.setText(path or '')
        self.output_dir_edit.blockSignals(False)

    def _sync_output_dir(self) -> None:
        if self.manager is not None:
            self.set_output_dir(self.manager.config.output.output_dir or '')

    def _on_output_dir_edited(self) -> None:
        if self.manager is None or self._loading:
            return
        path = self.output_dir_edit.text().strip()
        if not path:
            return
        current = self.manager.config.output.output_dir or ''
        try:
            same = os.path.abspath(path) == os.path.abspath(current)
        except Exception:
            same = path == current
        if not same:
            self.output_dir_changed.emit(path)

    def _pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, '选择输出目录（整理结果保存位置）',
            self.output_dir_edit.text().strip() or '')
        if path:
            self.output_dir_edit.setText(os.path.normpath(path))
            self._on_output_dir_edited()

    def _dest_hint(self, record: ImageRecord) -> str:
        """详情里展示"这张图最终会复制到哪里"。"""
        if record.dest_path:
            return record.dest_path
        name = record.final_filename or record.suggested_filename
        if not name or self.manager is None:
            return '（执行时按最终文件名确定）'
        return os.path.join(self.manager.config.output.output_dir or '', name)

    def refresh_records(self) -> None:
        """重新载入记录（OCR 完成或批量操作后调用）。"""
        if self.manager is None:
            return
        # 先算出最终文件名：这样"最终文件名"列和"将复制到"提示在进入本页时
        # 就有内容，而不是等执行时才出现一片空白。
        if self.manager.records and self.manager.config.output.output_dir:
            try:
                self.manager.rebuild_names(only_selected=True)
            except Exception:          # 目录不可写等异常不应阻断列表展示
                pass
        self.model.set_records(self.manager.records)
        self._sync_output_dir()
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

            dest = self._dest_hint(record)
            self.dest_edit.setText(dest)
            self.dest_edit.setToolTip(dest)
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
                       self.retry_failed_button, self.reocr_button,
                       self.output_dir_edit, self.output_dir_button):
            widget.setEnabled(not busy)
        self.execute_button.setText('正在处理…' if busy else '确认并执行复制 →')
        if not busy:
            self.force_stop_button.setVisible(False)
            self.reset_stop_button()

    # ================================================================ 进度

    def start_progress(self, total: int, message: str = '正在识别…') -> None:
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.force_stop_button.setVisible(True)
        self.force_stop_button.setEnabled(True)
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
        self.force_stop_button.setVisible(False)

    def set_stopping(self) -> None:
        """强制停止已发出：给出即时反馈并禁用按钮，避免重复点击。"""
        self.force_stop_button.setEnabled(False)
        self.force_stop_button.setText('正在停止…')
        self.progress_label.setText('正在强制停止…')

    def reset_stop_button(self) -> None:
        self.force_stop_button.setEnabled(True)
        self.force_stop_button.setText('强制停止')

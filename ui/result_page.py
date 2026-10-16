"""
第四步：执行与结果。

实时进度、暂停/继续/取消、日志、报告导出与撤销。
取消任务不会删除已复制的文件；撤销只删除本任务创建的目标文件。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from models import ProgressEvent, Status, TaskSummary
from ui.style import DANGER, SUCCESS, WARNING
from ui.widgets import make_hint


class ResultPage(QWidget):
    """执行进度与结果页。"""

    pause_requested = Signal()
    cancel_requested = Signal()
    undo_requested = Signal()
    export_requested = Signal()
    new_task_requested = Signal()
    open_output_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paused = False
        self._running = False
        self._build()

    # ================================================================ 界面

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 12)

        header = QHBoxLayout()
        title = QLabel('第四步 · 执行与结果')
        title.setObjectName('title')
        header.addWidget(title)
        header.addStretch(1)
        self.phase_label = QLabel('等待开始')
        self.phase_label.setObjectName('counter')
        header.addWidget(self.phase_label)
        layout.addLayout(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat('%p%  (%v / %m)')
        layout.addWidget(self.progress)

        self.busy_label = QLabel('准备就绪。')
        self.busy_label.setObjectName('hint')
        layout.addWidget(self.busy_label)

        # 计数器
        counter_group = QGroupBox('实时统计')
        counter_layout = QGridLayout(counter_group)
        self.counter_labels = {}
        items = [('current', '已处理'), ('success', '成功'), ('failed', '失败'),
                 ('skipped', '跳过'), ('conflicts', '重名改名')]
        for column, (key, label) in enumerate(items):
            counter_layout.addWidget(QLabel(label), 0, column)
            value_label = QLabel('0')
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value_label.setStyleSheet('font-size:18px; font-weight:bold; color:#245a91;')
            counter_layout.addWidget(value_label, 1, column)
            self.counter_labels[key] = value_label
        layout.addWidget(counter_group)

        # 当前处理项
        detail_group = QGroupBox('当前处理')
        detail_layout = QVBoxLayout(detail_group)
        self.source_label = QLabel('—')
        self.source_label.setWordWrap(True)
        self.ocr_label = QLabel('—')
        self.ocr_label.setWordWrap(True)
        self.dest_label = QLabel('—')
        self.dest_label.setWordWrap(True)
        for label, text in ((self.source_label, '来源：'),
                            (self.ocr_label, '识别：'),
                            (self.dest_label, '目标：')):
            row = QHBoxLayout()
            caption = QLabel(text)
            caption.setMinimumWidth(48)
            row.addWidget(caption)
            row.addWidget(label, 1)
            detail_layout.addLayout(row)
        layout.addWidget(detail_group)

        # 控制按钮
        controls = QHBoxLayout()
        self.pause_button = QPushButton('暂停')
        self.pause_button.clicked.connect(self.pause_requested.emit)
        controls.addWidget(self.pause_button)
        self.cancel_button = QPushButton('取消任务')
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        # 日志
        log_group = QGroupBox('处理日志')
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setStyleSheet('font-family: Consolas, "Microsoft YaHei"; font-size:12px;')
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 1)

        # 完成后的操作
        self.done_group = QGroupBox('任务完成')
        done_layout = QVBoxLayout(self.done_group)
        self.summary_label = QLabel('')
        self.summary_label.setWordWrap(True)
        done_layout.addWidget(self.summary_label)

        done_buttons = QHBoxLayout()
        for text, signal in (('打开输出目录', self.open_output_requested),
                             ('导出报告', self.export_requested),
                             ('撤销本次任务（仅删除本次生成的文件）', self.undo_requested),
                             ('开始新任务', self.new_task_requested)):
            button = QPushButton(text)
            if '撤销' in text:
                button.setObjectName('danger')
            button.clicked.connect(signal.emit)
            done_buttons.addWidget(button)
        done_buttons.addStretch(1)
        done_layout.addLayout(done_buttons)

        done_layout.addWidget(make_hint(
            '撤销只会删除"本次任务创建的目标文件"，并且会校验文件大小与记录一致；'
            '原始目录中的文件永远不会被删除。'))
        self.done_group.setVisible(False)
        layout.addWidget(self.done_group)

    # ================================================================ 控制

    def start(self, phase: str, total: int, message: str = '') -> None:
        """开始一个阶段。"""
        self._running = True
        self._paused = False
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.phase_label.setText(phase)
        self.busy_label.setText(message or '正在处理…')
        self.pause_button.setText('暂停')
        self.pause_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.done_group.setVisible(False)
        for label in self.counter_labels.values():
            label.setText('0')

    def set_total(self, total: int) -> None:
        self.progress.setRange(0, max(1, total))

    def on_progress(self, event: ProgressEvent) -> None:
        """处理进度事件。"""
        if event.total:
            self.progress.setRange(0, max(1, event.total))
            self.progress.setValue(min(event.current, event.total))
        self.counter_labels['current'].setText(str(event.current))
        if event.success:
            self.counter_labels['success'].setText(str(event.success))
        if event.failed:
            self.counter_labels['failed'].setText(str(event.failed))
        if event.skipped:
            self.counter_labels['skipped'].setText(str(event.skipped))

        if event.source:
            self.source_label.setText(event.source)
        if event.ocr_text:
            self.ocr_label.setText(event.ocr_text)
        if event.dest:
            self.dest_label.setText(event.dest)
        if event.message:
            self.busy_label.setText(event.message)

    def set_conflicts(self, count: int) -> None:
        self.counter_labels['conflicts'].setText(str(count))

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText('继续' if paused else '暂停')
        self.busy_label.setText('已暂停，点击"继续"恢复处理…' if paused else '正在处理…')

    def finish(self, summary: TaskSummary, title: str = '任务完成',
               extra_html: str = '') -> None:
        """结束一个阶段。"""
        self._running = False
        self._paused = False
        self.phase_label.setText(title)
        self.busy_label.setText('处理结束。')
        self.pause_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(self.progress.maximum())

        lines = [
            f'<b>文件总数：</b>{summary.total}　'
            f'<b>成功：</b><span style="color:{SUCCESS}">{summary.success}</span>　'
            f'<b>失败：</b><span style="color:{DANGER}">{summary.failed}</span>　'
            f'<b>跳过：</b>{summary.skipped}　'
            f'<b>需人工确认：</b><span style="color:{WARNING}">{summary.needs_review}</span>',
            f'<b>重名改名：</b>{summary.conflicts}　'
            f'<b>OCR 成功：</b>{summary.ocr_ok}　'
            f'<b>OCR 失败：</b>{summary.ocr_failed}　'
            f'<b>耗时：</b>{summary.elapsed_text}',
        ]
        if extra_html:
            lines.append(extra_html)
        self.summary_label.setText('<br>'.join(lines))
        self.done_group.setVisible(True)
        self.set_conflicts(summary.conflicts)

    def show_undo_result(self, result: dict) -> None:
        text = (f'撤销完成：已删除 {result.get("deleted", 0)} 个文件，'
                f'跳过 {result.get("skipped", 0)}，失败 {result.get("failed", 0)}。')
        if result.get('missing'):
            text += f' 另有 {result["missing"]} 个文件已不存在。'
        self.append_log(text)
        errors = result.get('errors') or []
        for path, error in errors[:20]:
            self.append_log(f'  未能删除：{path} -> {error}')
        QMessageBox.information(self, '撤销完成', text)

    # ================================================================ 事件

    def _on_cancel_clicked(self) -> None:
        answer = QMessageBox.question(
            self, '确认取消任务',
            '取消后将停止处理剩余文件。\n\n'
            '已经成功复制的文件会保留（不会被删除），原始文件不受影响。\n\n确定取消吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.cancel_requested.emit()

    @property
    def running(self) -> bool:
        return self._running

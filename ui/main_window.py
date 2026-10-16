"""
主窗口：向导容器 + 任务编排 + 历史记录。

界面线程只负责显示与转发，所有耗时操作都交给 ui/workers.py 里的 QThread。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core.task_manager import TaskError, TaskManager
from database.database import get_database
from models import Status, TaskConfig
from ui.ocr_page import OcrPage
from ui.result_page import ResultPage
from ui.scan_page import ScanPage
from ui.settings_page import SettingsPage
from ui.widgets import StepBar
from ui.workers import (ExecuteWorker, OcrWorker, ScanWorker, SingleOcrWorker,
                        UndoWorker)
from utils import file_utils
from utils.config import get_config
from utils.logger import TaskLogger, get_logger

logger = get_logger('ui')

STEPS = ['设置任务', '扫描结果', 'OCR 与命名', '执行与结果']


class LogRelay(QObject):
    """把工作线程里的日志安全地转到界面线程。"""

    message = Signal(str)


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle('图片智能提取与 OCR 命名工具')
        self.resize(1360, 880)
        self.setMinimumSize(1080, 700)

        self.app_config = get_config()
        self.database = get_database()
        self.manager: TaskManager | None = None
        self.worker = None                      # 当前后台线程（由 finished 信号释放）
        self.task_config: TaskConfig = self.app_config.load_task_config()
        self.log_relay = LogRelay()
        self.log_relay.message.connect(self._append_log)

        self._build()
        self._connect()
        self.settings_page.load_config(self.task_config)
        self._update_status('请先设置任务，然后点击"扫描文件"。')

    # ================================================================ 构建

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self.step_bar = StepBar(STEPS)
        layout.addWidget(self.step_bar)

        self.stack = QStackedWidget()
        self.settings_page = SettingsPage()
        self.scan_page = ScanPage()
        self.ocr_page = OcrPage()
        self.result_page = ResultPage()
        for page in (self.settings_page, self.scan_page, self.ocr_page,
                     self.result_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        self._build_menu()
        self.status = self.statusBar()
        self.status_label = QLabel('')
        self.status.addPermanentWidget(self.status_label)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu('文件(&F)')
        action = QAction('打开输出目录', self)
        action.triggered.connect(
            lambda: file_utils.open_in_explorer(self.task_config.output.output_dir))
        file_menu.addAction(action)

        action = QAction('打开日志目录', self)
        action.triggered.connect(lambda: file_utils.open_in_explorer(
            file_utils.get_log_dir()))
        file_menu.addAction(action)

        action = QAction('打开数据目录', self)
        action.triggered.connect(lambda: file_utils.open_in_explorer(
            file_utils.get_app_data_dir()))
        file_menu.addAction(action)

        file_menu.addSeparator()
        action = QAction('退出', self)
        action.triggered.connect(self.close)
        file_menu.addAction(action)

        tool_menu = self.menuBar().addMenu('工具(&T)')
        action = QAction('任务历史', self)
        action.triggered.connect(self.show_history)
        tool_menu.addAction(action)

        action = QAction('检测 OCR 环境', self)
        action.triggered.connect(self._show_engine_status)
        tool_menu.addAction(action)

        help_menu = self.menuBar().addMenu('帮助(&H)')
        action = QAction('使用说明', self)
        action.triggered.connect(self._show_help)
        help_menu.addAction(action)

        action = QAction('关于', self)
        action.triggered.connect(self._show_about)
        help_menu.addAction(action)

    def _connect(self) -> None:
        self.settings_page.scan_requested.connect(self.on_scan)
        self.scan_page.back_requested.connect(lambda: self._goto(0))
        self.scan_page.rescan_requested.connect(self.on_scan)
        self.scan_page.ocr_requested.connect(self.on_ocr)

        self.ocr_page.back_requested.connect(lambda: self._goto(1))
        self.ocr_page.execute_requested.connect(self.on_execute)
        self.ocr_page.reocr_requested.connect(self.on_reocr)

        self.result_page.pause_requested.connect(self.on_pause)
        self.result_page.cancel_requested.connect(self.on_cancel)
        self.result_page.undo_requested.connect(self.on_undo)
        self.result_page.export_requested.connect(self.on_export)
        self.result_page.new_task_requested.connect(self.on_new_task)
        self.result_page.open_output_requested.connect(
            lambda: file_utils.open_in_explorer(self.task_config.output.output_dir))

    # ================================================================ 导航

    def _goto(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.step_bar.set_step(index)

    def _update_status(self, text: str) -> None:
        self.status.showMessage(text)
        self.status_label.setText(text)

    def _append_log(self, text: str) -> None:
        self.result_page.append_log(text)

    # ================================================================ 扫描

    def on_scan(self) -> None:
        if not self._idle():
            return
        try:
            self.task_config = self.settings_page.collect()
        except ValueError as exc:
            QMessageBox.warning(self, '设置不完整', str(exc))
            return

        if not self._check_output_dir():
            return

        self.app_config.save_task_config(self.task_config)
        self.app_config.add_recent_root(self.task_config.rule.root_dir)

        if self.manager is not None:
            self.manager.close()
        self.manager = TaskManager(
            self.task_config, self.database,
            TaskLogger('当前任务', echo=self.log_relay.message.emit))
        self.manager.task_id = self.database.create_task(
            root_dir=self.task_config.rule.root_dir,
            template=self.manager.template_text,
            output_dir=self.task_config.output.output_dir,
            naming_fields=self.task_config.naming.fields,
            config=self.task_config.to_dict(),
            log_path=self.manager.log.path if self.manager.log else '')

        self.scan_page.set_manager(self.manager)
        self.ocr_page.set_manager(self.manager)
        self.settings_page.set_busy(True)
        self._update_status('正在扫描文件系统（只读，不会修改任何文件）…')
        self.result_page.append_log(
            f'[{Status.label("pending")}] 开始扫描：{self.manager.template_text}')

        self.worker = ScanWorker(self.manager)
        self.worker.progress.connect(self._on_scan_progress)
        self.worker.done.connect(self._on_scan_done)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_scan_progress(self, event) -> None:
        found = event.extra.get('found', 0) if event.extra else 0
        self._update_status(f'正在扫描… 已访问 {event.current} 个目录，'
                            f'已找到 {found} 个匹配文件')

    def _on_scan_done(self, result) -> None:
        self.settings_page.set_busy(False)
        if not hasattr(result, 'files'):
            return
        self.scan_page.show_result(result, self.manager.template_text,
                                   self.task_config.output.output_dir)
        self._goto(1)
        self._update_status(f'扫描完成：找到 {result.count} 个匹配文件')
        self.manager.save_to_db('scanned')
        self.database.update_task(self.manager.task_id, total=result.count)

    def _on_worker_finished(self) -> None:
        """QThread 真正结束后再释放引用（避免"线程仍在运行时被销毁"）。"""
        worker = self.sender()
        if worker is self.worker:
            self.worker = None

    def _on_worker_failed(self, message: str) -> None:
        self.settings_page.set_busy(False)
        self.scan_page.set_busy(False)
        self.ocr_page.set_busy(False)
        self._update_status('任务失败')
        QMessageBox.critical(self, '任务失败', message)

    # ================================================================ OCR

    def on_ocr(self, only: str = '') -> None:
        if self.manager is None or not self._idle():
            return
        records = self.manager.records
        if not records:
            QMessageBox.information(self, '没有可处理的文件', '请先扫描并确认文件清单。')
            return

        self.ocr_page.refresh_records()
        self._goto(2)
        self.ocr_page.set_busy(True)
        self.ocr_page.start_progress(len(records), '正在加载 OCR 引擎…')
        self._update_status('正在进行本地 OCR 识别（图片不会离开本机）…')

        self.worker = OcrWorker(self.manager, only=only)
        self.worker.progress.connect(self._on_ocr_progress)
        self.worker.done.connect(self._on_ocr_done)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_ocr_progress(self, event) -> None:
        self.ocr_page.set_progress(event.current, event.total,
                                   f'{event.current}/{event.total} '
                                   f'{os.path.basename(event.source or "")}')
        if event.current % 5 == 0 or event.current == event.total:
            self.ocr_page.update_record_by_index(event.current - 1)

    def _on_ocr_done(self, summary) -> None:
        self.ocr_page.set_busy(False)
        self.ocr_page.end_progress('识别完成')
        self.ocr_page.refresh_records()
        self._update_status(
            f'OCR 完成：成功 {summary.ocr_ok}，失败 {summary.ocr_failed}，'
            f'需人工确认 {summary.needs_review}')
        self.manager.save_to_db('ocr_done')

    def on_reocr(self, record) -> None:
        """单张重新 OCR。"""
        if self.manager is None or not self._idle():
            return
        self.ocr_page.set_busy(True)
        self._update_status(f'正在重新识别第 {record.index} 张…')
        self.worker = SingleOcrWorker(self.manager, record)
        self.worker.done.connect(self._on_reocr_done)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_reocr_done(self, record) -> None:
        self.ocr_page.set_busy(False)
        self.ocr_page.update_record(record)
        self.ocr_page.refresh_records()
        self._update_status(f'第 {record.index} 张识别完成')

    # ================================================================ 执行

    def on_execute(self, include_unreviewed: bool) -> None:
        if self.manager is None or not self._idle():
            return

        try:
            self.manager.rebuild_names()
        except TaskError as exc:
            QMessageBox.warning(self, '无法执行', str(exc))
            return

        check = self.manager.precheck(include_unreviewed)
        if not check['ok']:
            QMessageBox.warning(self, '执行前检查未通过',
                                '\n'.join(check['problems']))
            return

        eligible = [r for r in self.manager.records
                    if r.status in (Status.READY, Status.OCR_DONE)
                    or (include_unreviewed and r.status == Status.NEEDS_REVIEW)]
        if not eligible:
            QMessageBox.information(self, '没有可复制的文件',
                                    '当前没有处于"就绪"状态的文件。\n'
                                    '请先完成 OCR 并确认命名，或勾选"包含未经确认的文件"。')
            return

        conflicts = sum(1 for r in eligible if r.conflict)
        total_size = sum(r.size for r in eligible)
        text = (
            f'即将复制 <b>{len(eligible)}</b> 个文件到：<br>{self.task_config.output.output_dir}<br><br>'
            f'预计占用空间：{file_utils.human_size(total_size)}<br>'
            f'目标磁盘剩余：{file_utils.human_size(check["free_space"])}<br>'
            f'其中重名改名：{conflicts} 个<br><br>'
            f'<b>原始文件不会被修改、移动或删除。</b>')
        if check['problems']:
            text += '<br><br><span style="color:#c47f0a">注意：' + \
                    '<br>'.join(check['problems']) + '</span>'

        if not self._confirm_execute(text):
            return

        self._goto(3)
        self.ocr_page.set_busy(True)
        self.result_page.start('正在复制文件', len(eligible),
                               '开始复制文件…（可随时暂停或取消）')
        self._update_status('正在复制文件…')

        self.worker = ExecuteWorker(self.manager, include_unreviewed)
        self.worker.progress.connect(self._on_execute_progress)
        self.worker.log.connect(self._append_log)
        self.worker.done.connect(self._on_execute_done)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _confirm_execute(self, html_text: str) -> bool:
        """执行前确认对话框（独立方法，便于自动化测试替换）。"""
        box = QMessageBox(self)
        box.setWindowTitle('确认执行')
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(html_text)
        box.setIcon(QMessageBox.Icon.Question)
        yes = box.addButton('开始复制', QMessageBox.ButtonRole.AcceptRole)
        box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _on_execute_progress(self, event) -> None:
        self.result_page.on_progress(event)
        self._update_status(
            f'正在复制 {event.current}/{event.total}　成功 {event.success}　'
            f'失败 {event.failed}')

    def _on_execute_done(self, result) -> None:
        self.ocr_page.set_busy(False)
        summary = result.summary
        self.manager.save_to_db('finished')
        self.database.update_task(
            self.manager.task_id, success=summary.success, failed=summary.failed,
            skipped=summary.skipped, needs_review=summary.needs_review)

        report_paths = []
        if self.task_config.output.write_report and self.task_config.output.report_formats:
            try:
                report_paths = self.manager.export_reports(summary)
            except Exception as exc:
                logger.exception('导出报告失败')
                self._append_log(f'导出报告失败：{exc}')

        extra = ''
        if report_paths:
            extra = '报告：' + '　'.join(os.path.basename(p) for p in report_paths)
        if summary.failed:
            extra += f'<br><span style="color:#c0392b">有 {summary.failed} 个文件失败，' \
                     f'详情见报告与日志。</span>'

        self.result_page.finish(summary, '复制完成', extra)
        self._update_status(
            f'完成：成功 {summary.success}，失败 {summary.failed}，'
            f'跳过 {summary.skipped}，耗时 {summary.elapsed_text}')

        if result.errors:
            self._append_log('—' * 30)
            self._append_log('失败明细：')
            for path, error in result.errors[:100]:
                if error:
                    self._append_log(f'  {path} -> {error}')

    # ================================================================ 控制

    def on_pause(self) -> None:
        if self.worker is None:
            return
        if self.worker.paused:
            self.worker.resume()
            self.result_page.set_paused(False)
            self._update_status('已继续')
        else:
            self.worker.pause()
            self.result_page.set_paused(True)
            self._update_status('已暂停')

    def on_cancel(self) -> None:
        if self.worker is None:
            return
        self.worker.cancel()
        self._update_status('正在取消…已复制的文件会保留')
        self._append_log('用户请求取消任务…')

    def on_undo(self) -> None:
        if self.manager is None or self.manager.task_id is None or not self._idle():
            QMessageBox.information(self, '无法撤销', '当前没有可撤销的任务。')
            return
        rows = self.database.created_files(self.manager.task_id)
        if not rows:
            QMessageBox.information(self, '无需撤销',
                                    '本任务没有创建任何文件（或已全部撤销）。')
            return

        answer = QMessageBox.question(
            self, '确认撤销',
            f'本任务创建了 {len(rows)} 个文件。\n\n'
            f'是否删除本次任务生成的文件？\n\n'
            f'• 只会删除输出目录中由本任务新建的文件\n'
            f'• 会校验文件大小与记录一致，不一致的会跳过\n'
            f'• 原始目录中的文件绝对不会被删除\n\n'
            f'确定要删除吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.result_page.start('正在撤销', len(rows), '正在删除本任务生成的文件…')
        self.worker = UndoWorker(self.manager, self.manager.task_id)
        self.worker.progress.connect(self._on_undo_progress)
        self.worker.done.connect(self._on_undo_done)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_undo_progress(self, event) -> None:
        self.result_page.on_progress(event)

    def _on_undo_done(self, result) -> None:
        self.result_page.show_undo_result(result)
        self._update_status(f'撤销完成：删除 {result.get("deleted", 0)} 个文件')

    def on_export(self) -> None:
        if self.manager is None:
            return
        try:
            paths = self.manager.export_reports()
        except Exception as exc:
            QMessageBox.warning(self, '导出失败', str(exc))
            return
        if not paths:
            QMessageBox.information(self, '没有导出内容',
                                    '请先在设置中勾选报告格式。')
            return
        QMessageBox.information(self, '报告已生成',
                                '已生成：\n' + '\n'.join(paths))
        file_utils.open_in_explorer(os.path.dirname(paths[0]))

    def on_new_task(self) -> None:
        if not self._idle():
            QMessageBox.information(self, '请稍候', '当前任务尚未结束。')
            return
        self.scan_page.model.set_records([])
        self.ocr_page.model.set_records([])
        self.result_page.log_view.clear()
        self._goto(0)
        self._update_status('已返回设置页，可开始新任务。')

    # ================================================================ 辅助

    def _idle(self) -> bool:
        """当前是否没有正在运行的后台任务。"""
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, '请稍候', '当前有任务正在执行，请先等待完成或取消。')
            return False
        return True

    def _check_output_dir(self) -> bool:
        """输出目录检查。**不会创建任何目录**（保持扫描阶段零写入）。"""
        output = self.task_config.output.output_dir
        root = self.task_config.rule.root_dir

        if not os.path.isdir(output):
            # 目录还不存在时，检查最近的已存在祖先是否可写
            probe = os.path.dirname(os.path.abspath(output))
            while probe and not os.path.isdir(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            if not probe or not os.path.isdir(probe):
                QMessageBox.warning(self, '输出目录不可用',
                                    f'输出目录的上级目录不存在：\n{output}')
                return False
            if not os.access(probe, os.W_OK):
                QMessageBox.warning(self, '输出目录不可用',
                                    f'没有写入权限：\n{probe}')
                return False

        if os.path.abspath(output) == os.path.abspath(root):
            QMessageBox.warning(self, '输出目录不合法', '输出目录不能与根目录相同。')
            return False

        out_key = file_utils.norm_path_key(output)
        root_key = file_utils.norm_path_key(root)
        if out_key.startswith(root_key.rstrip('/') + '/'):
            answer = QMessageBox.question(
                self, '输出目录位于根目录内部',
                '输出目录在根目录内部。\n\n'
                '本次任务没有问题，但下次重新扫描时，输出目录里的文件也可能会被扫描到。\n'
                '建议把输出目录放在根目录之外。\n\n是否继续？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            return answer == QMessageBox.StandardButton.Yes
        return True

    def _show_engine_status(self) -> None:
        from ocr.engine import engine_status
        status = engine_status()
        lines = []
        for key, info in status.items():
            mark = '✔ 可用' if info.get('available') else '✘ 不可用'
            lines.append(f'{mark}　{info.get("name", key)}'
                         + (f'　（{info.get("error")}）' if info.get('error') else ''))
        lines.append('')
        lines.append('说明：所有 OCR 都在本机运行，图片与识别结果不会上传到任何服务器。')
        lines.append('如需更高精度，可在 Python 3.11/3.12 环境执行：')
        lines.append('    pip install paddlepaddle paddleocr')
        QMessageBox.information(self, 'OCR 环境', '\n'.join(lines))
        self.settings_page.refresh_engine_status()

    def _show_help(self) -> None:
        text = (
            '<b>使用流程</b><br>'
            '1. <b>设置任务</b>：选择根目录 → 设置路径规则（普通模式选下拉框，'
            '高级模式直接写模板，如 <code>*/1440/SAMPLE_NA.jpg</code>）'
            '→ 选择输出目录 → 设置命名字段。<br>'
            '2. <b>扫描</b>：只读取文件信息，列出所有匹配的完整路径，'
            '由您确认后才会继续。<br>'
            '3. <b>OCR 与命名</b>：在源图片上做本地 OCR，按'
            '"指定字段 → 本地 AI → 人工确认"生成文件名。<br>'
            '4. <b>执行</b>：复制文件（不是移动），重名自动追加 -1/-2/-3，'
            '绝不覆盖已有文件。<br><br>'
            '<b>安全承诺</b><br>'
            '• 原始文件永远保持原样（只读扫描 + 只复制）<br>'
            '• 目标目录已有文件永远不会被覆盖<br>'
            '• 单个文件失败不会中断整个任务<br>'
            '• 全程离线，不上传任何图片或文字<br><br>'
            '<b>路径模板语法</b><br>'
            '• <code>*</code>：匹配恰好一层目录名<br>'
            '• <code>**</code>：匹配任意层级目录<br>'
            '• 最后一段是文件名，可用通配符，如 <code>*.jpg</code>')
        box = QMessageBox(self)
        box.setWindowTitle('使用说明')
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self, '关于',
            '<b>图片智能提取与 OCR 命名工具</b><br><br>'
            '· 严格按路径层级提取图片，扫描与执行分离<br>'
            '· 本地 OCR（PaddleOCR / RapidOCR），全程离线<br>'
            '· 只复制不移动，绝不覆盖已有文件<br>'
            '· 支持重名自动编号、人工确认、任务历史与撤销<br><br>'
            f'数据目录：{file_utils.get_app_data_dir()}')

    # ================================================================ 历史

    def show_history(self) -> None:
        dialog = HistoryDialog(self.database, self)
        dialog.exec()

    # ================================================================ 关闭

    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, '任务正在运行',
                '当前有任务正在执行。确定要退出吗？\n\n'
                '已复制的文件会保留，原始文件不受影响。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)

        if self.manager is not None:
            try:
                self.manager.close()
            except Exception:
                pass
        try:
            self.database.close()
        except Exception:
            pass
        self.app_config.save()
        event.accept()


class HistoryDialog(QDialog):
    """任务历史。"""

    COLUMNS = ['编号', '时间', '根目录', '匹配规则', '总数', '成功', '失败',
               '输出目录']

    def __init__(self, database, parent=None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle('任务历史')
        self.resize(1000, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            '历史记录保存在本机 SQLite 数据库中（%s）。' % file_utils.get_db_path()))

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.reload)
        buttons.addWidget(refresh)
        open_output = QPushButton('打开输出目录')
        open_output.clicked.connect(self._open_output)
        buttons.addWidget(open_output)
        delete = QPushButton('删除该记录')
        delete.clicked.connect(self._delete)
        buttons.addWidget(delete)
        buttons.addStretch(1)
        close = QPushButton('关闭')
        close.setObjectName('primary')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.reload()

    def reload(self) -> None:
        tasks = self.database.list_tasks(limit=500)
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [
                task.get('id'), task.get('created_at'), task.get('root_dir'),
                task.get('template'), task.get('total'), task.get('success'),
                task.get('failed'), task.get('output_dir'),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem('' if value is None else str(value))
                item.setToolTip('' if value is None else str(value))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

    def _current_task_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.text()) if item else None

    def _open_output(self) -> None:
        task_id = self._current_task_id()
        if task_id is None:
            return
        task = self.database.get_task(task_id)
        if task and task.get('output_dir'):
            file_utils.open_in_explorer(task['output_dir'])

    def _delete(self) -> None:
        task_id = self._current_task_id()
        if task_id is None:
            return
        answer = QMessageBox.question(
            self, '删除记录',
            f'仅删除数据库中的历史记录（编号 {task_id}），'
            f'不会删除任何已复制的文件。\n\n确定吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.database.delete_task(task_id)
            self.reload()

"""
可复用界面组件：

- 图像转换工具（PIL -> QPixmap；用 PIL 读图以支持中文路径）
- RecordTableModel：支持数万行的高性能表格模型
- RecordFilterProxy：文本 + 状态筛选、自然排序
- ThumbnailProvider：后台线程池加载缩略图（带 LRU 缓存）
- StepBar：向导步骤条
- ImagePreview：图片预览面板
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QSize,
                            QRunnable, Qt, QThreadPool, Signal)
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                               QVBoxLayout, QWidget)

from models import ImageRecord, Status
from ui.style import MUTED, STATUS_COLORS


# ==================================================================== 图像工具

def pil_to_qimage(image) -> QImage:
    """PIL.Image -> QImage（RGB888）。"""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    data = image.tobytes('raw', 'RGB')
    qimage = QImage(data, image.width, image.height, image.width * 3,
                    QImage.Format.Format_RGB888)
    return qimage.copy()


def load_qimage(path: str, max_side: int = 0) -> QImage | None:
    """读取图片为 QImage（支持中文路径与 EXIF 方向）。可在工作线程中调用。"""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if max_side and max(image.size) > max_side:
                image.thumbnail((max_side, max_side), Image.LANCZOS)
            return pil_to_qimage(image)
    except Exception:
        return None


def load_qpixmap(path: str, max_side: int = 0) -> QPixmap | None:
    """读取图片为 QPixmap（只能在 GUI 线程中调用）。"""
    image = load_qimage(path, max_side)
    return QPixmap.fromImage(image) if image is not None else None


# ==================================================================== 表格模型

@dataclass
class Column:
    """表格列定义。"""
    key: str
    title: str
    width: int = 120
    kind: str = 'text'          # text | check | thumb | status | path


#: 扫描页列
SCAN_COLUMNS = [
    Column('check', '', 34, 'check'),
    Column('index', '序号', 60),
    Column('thumb', '缩略图', 76, 'thumb'),
    Column('source_folder', '来源文件夹', 130),
    Column('target_dir', '目标层', 90),
    Column('original_name', '原始文件名', 190),
    Column('rel_path', '完整相对路径', 330, 'path'),
    Column('status', '状态', 90, 'status'),
]

#: OCR 页列
OCR_COLUMNS = [
    Column('check', '', 34, 'check'),
    Column('index', '序号', 60),
    Column('source_folder', '来源文件夹', 120),
    Column('ocr_text', 'OCR 结果', 280),
    Column('extracted_name', '识别名称', 130),
    Column('method', '方式', 80),
    Column('final_filename', '最终文件名', 260),
    Column('status', '状态', 100, 'status'),
]

#: 结果页列
RESULT_COLUMNS = [
    Column('index', '序号', 60),
    Column('source_path', '来源路径', 340, 'path'),
    Column('extracted_name', '识别名称', 130),
    Column('final_filename', '最终文件名', 240),
    Column('detail', '重名处理', 200),
    Column('status', '状态', 100, 'status'),
    Column('error', '错误', 220),
]


class RecordTableModel(QAbstractTableModel):
    """图片记录表格模型。"""

    select_toggled = Signal(object)      # ImageRecord

    def __init__(self, columns: list[Column], records: list | None = None,
                 parent=None, target_layer: str = ''):
        super().__init__(parent)
        self.columns = columns
        self.records: list[ImageRecord] = []
        self._row_by_path: dict[str, int] = {}
        self.target_layer = target_layer
        self.thumbnails: 'ThumbnailProvider | None' = None
        if records:
            self.set_records(records)

    # --------------------------------------------------

    def set_records(self, records: list) -> None:
        self.beginResetModel()
        self.records = list(records or [])
        # 路径 -> 行号索引：缩略图加载完成后只刷新对应那一行，
        # 几万行的表格也不会因为缩略图而整表重绘
        self._row_by_path = {r.source_path: i for i, r in enumerate(self.records)}
        self.endResetModel()

    def row_of_path(self, path: str) -> int:
        return self._row_by_path.get(path, -1)

    def refresh(self, row: int = -1) -> None:
        """刷新整表或某一行。"""
        if row < 0 or row >= len(self.records):
            if self.records:
                top = self.index(0, 0)
                bottom = self.index(len(self.records) - 1, len(self.columns) - 1)
                self.dataChanged.emit(top, bottom)
            return
        left = self.index(row, 0)
        right = self.index(row, len(self.columns) - 1)
        self.dataChanged.emit(left, right)

    def record_at(self, row: int) -> ImageRecord | None:
        if 0 <= row < len(self.records):
            return self.records[row]
        return None

    def searchable_text(self, record: ImageRecord) -> str:
        return ' '.join([
            str(record.index), record.source_folder or '', record.original_name or '',
            record.rel_path or '', record.ocr_text or '', record.extracted_name or '',
            record.final_filename or '', Status.label(record.status), record.error or '',
        ]).lower()

    # -------------------------------------------------- Qt 接口

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section].title
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        column = self.columns[index.column()]
        if column.kind == 'check':
            return base | Qt.ItemFlag.ItemIsUserCheckable
        if column.key == 'final_filename':
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self.records[index.row()]
        column = self.columns[index.column()]
        key = column.key

        if role == Qt.ItemDataRole.CheckStateRole and column.kind == 'check':
            checked = record.status not in (Status.SKIPPED,)
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.DecorationRole and column.kind == 'thumb':
            return self._thumbnail(record)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self._display(record, column, role)

        if role == Qt.ItemDataRole.ForegroundRole and column.kind == 'status':
            color = STATUS_COLORS.get(record.status)
            return QColor(color) if color else None

        if role == Qt.ItemDataRole.TextAlignmentRole and column.kind in ('check', 'status'):
            return int(Qt.AlignmentFlag.AlignCenter)

        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False
        record = self.records[index.row()]
        column = self.columns[index.column()]

        if role == Qt.ItemDataRole.CheckStateRole and column.kind == 'check':
            checked = (Qt.CheckState(value) == Qt.CheckState.Checked)
            self.select_toggled.emit((record, checked))
            return True

        if role == Qt.ItemDataRole.EditRole and column.key == 'final_filename':
            text = str(value or '').strip()
            if text != record.final_filename:
                record.final_filename = text
                record.manual_override = bool(text)
                self.dataChanged.emit(index, index)
            return True
        return False

    # --------------------------------------------------

    def _display(self, record: ImageRecord, column: Column, role):
        key = column.key
        if key == 'index':
            return record.index
        if key == 'source_folder':
            return record.source_folder
        if key == 'original_name':
            return record.original_name
        if key == 'rel_path':
            return record.rel_path
        if key == 'source_path':
            return record.source_path
        if key == 'target_dir':
            # 文件所在的那一层目录名（例如规则 */1440/x.jpg 中的 "1440"）
            parts = (record.rel_path or '').split('/')
            return parts[-2] if len(parts) >= 2 else ''
        if key == 'ocr_text':
            text = (record.ocr_text or '').replace('\n', ' | ')
            return text[:200]
        if key == 'extracted_name':
            return record.extracted_name or ''
        if key == 'method':
            from models import ExtractMethod
            return ExtractMethod.LABELS.get(record.extract_method,
                                            record.extract_method or '')
        if key == 'final_filename':
            return record.final_filename or record.suggested_filename or ''
        if key == 'status':
            return Status.label(record.status)
        if key == 'detail':
            return record.conflict_note if record.conflict else ''
        if key == 'error':
            return record.error or ''
        return ''

    def _thumbnail(self, record: ImageRecord):
        if self.thumbnails is None:
            return None
        return self.thumbnails.get(record.source_path)


class RecordFilterProxy(QObject):
    """文本 + 状态筛选 + 自然排序的代理模型。"""

    #: 特殊筛选值：只看需人工确认的
    STATUS_REVIEW = '__review__'

    def __init__(self, model: RecordTableModel, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QSortFilterProxyModel

        self.model = model
        self.text = ''
        self.status_filter = ''

        owner = self

        class _Proxy(QSortFilterProxyModel):
            def filterAcceptsRow(self, row, parent_index):
                record = owner.model.record_at(row)
                if record is None:
                    return False
                if owner.status_filter == owner.STATUS_REVIEW:
                    if record.status != Status.NEEDS_REVIEW:
                        return False
                elif owner.status_filter and record.status != owner.status_filter:
                    return False
                if owner.text and owner.text not in owner.model.searchable_text(record):
                    return False
                return True

            def lessThan(self, left, right):
                from core.path_matcher import natural_key
                lrec = owner.model.record_at(left.row())
                rrec = owner.model.record_at(right.row())
                if lrec is None or rrec is None:
                    return False
                column = owner.model.columns[left.column()]
                if column.key == 'index':
                    return lrec.index < rrec.index
                lval = owner.model._display(lrec, column, Qt.ItemDataRole.DisplayRole)
                rval = owner.model._display(rrec, owner.model.columns[right.column()],
                                            Qt.ItemDataRole.DisplayRole)
                return natural_key(str(lval)) < natural_key(str(rval))

        self.proxy = _Proxy()
        self.proxy.setSourceModel(model)
        self.proxy.setDynamicSortFilter(True)

    def set_text(self, text: str) -> None:
        self.text = (text or '').strip().lower()
        self.proxy.invalidateFilter()

    def set_status(self, status: str) -> None:
        self.status_filter = status or ''
        self.proxy.invalidateFilter()

    def record_at_proxy_row(self, row: int) -> ImageRecord | None:
        source_index = self.proxy.mapToSource(self.proxy.index(row, 0))
        return self.model.record_at(source_index.row())


# ==================================================================== 缩略图

class _ThumbSignals(QObject):
    #: (路径, QImage 或 None) —— QImage 可以安全地跨线程传递
    ready = Signal(str, object)


class _ThumbTask(QRunnable):
    """工作线程里的缩略图加载任务（只做 CPU/IO，不碰 QPixmap）。"""

    def __init__(self, path: str, size: int, signals: _ThumbSignals):
        super().__init__()
        self.path = path
        self.size = size
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        image = load_qimage(self.path, self.size)
        try:
            self.signals.ready.emit(self.path, image)
        except RuntimeError:      # 窗口已销毁
            pass


class ThumbnailProvider(QObject):
    """后台加载缩略图，带 LRU 缓存，避免界面卡顿。"""

    updated = Signal(str)

    def __init__(self, size: int = 64, limit: int = 800, parent=None):
        super().__init__(parent)
        self.size = size
        self.limit = limit
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._missing: set[str] = set()
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_ready)     # 自动排队回 GUI 线程
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    # -------------------------------------------------- GUI 线程

    def _on_ready(self, path: str, image) -> None:
        """在主线程中建立 QPixmap 并通知视图刷新。"""
        self._pending.discard(path)
        if image is None:
            self._missing.add(path)
        else:
            self._cache[path] = QPixmap.fromImage(image)
            while len(self._cache) > self.limit:
                self._cache.popitem(last=False)
        self.updated.emit(path)

    def get(self, path: str):
        """获取缩略图；未加载时返回 None 并排队后台加载。"""
        if not path:
            return None
        cached = self._cache.get(path)
        if cached is not None:
            self._cache.move_to_end(path)
            return cached
        if path in self._missing or path in self._pending:
            return None
        self._pending.add(path)
        self._pool.start(_ThumbTask(path, self.size, self._signals))
        return None

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()
        self._missing.clear()

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)


# ==================================================================== 步骤条

class StepBar(QWidget):
    """向导步骤指示条。"""

    def __init__(self, titles: list[str], parent=None):
        super().__init__(parent)
        self.titles = titles
        self.current = 0
        self.labels: list[QLabel] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(6)
        for i, title in enumerate(titles):
            label = QLabel(f'{i + 1}. {title}')
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(30)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.labels.append(label)
            layout.addWidget(label)
            if i < len(titles) - 1:
                arrow = QLabel('›')
                arrow.setStyleSheet(f'color: {MUTED}; font-size: 18px;')
                layout.addWidget(arrow)
        self.set_step(0)

    def set_step(self, index: int) -> None:
        self.current = index
        for i, label in enumerate(self.labels):
            if i == index:
                label.setStyleSheet(
                    'background:#2f6fb0; color:white; border-radius:4px; font-weight:bold;')
            elif i < index:
                label.setStyleSheet(
                    'background:#e8f1fa; color:#245a91; border-radius:4px;')
            else:
                label.setStyleSheet(
                    'background:#f2f4f6; color:#9aa3ad; border-radius:4px;')


# ==================================================================== 预览

class ImagePreview(QFrame):
    """图片预览面板。"""

    def __init__(self, parent=None, title: str = '图片预览'):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._pixmap: QPixmap | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel(title)
        self.title.setObjectName('title')
        self.view = QLabel('未选择图片')
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(280, 240)
        self.view.setStyleSheet('background:#fafbfc; border:1px solid #e3e7eb; color:#9aa3ad;')
        self.info = QLabel('')
        self.info.setObjectName('hint')
        self.info.setWordWrap(True)

        layout.addWidget(self.title)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.info)

    def show_path(self, path: str, info: str = '') -> None:
        pixmap = load_qpixmap(path, 1400)
        if pixmap is None:
            self.view.setText('无法预览该图片')
            self.view.setPixmap(QPixmap())
            self._pixmap = None
        else:
            self._pixmap = pixmap
            self._rescale()
        self.info.setText(info or (path if path else ''))

    def clear(self) -> None:
        self._pixmap = None
        self.view.setPixmap(QPixmap())
        self.view.setText('未选择图片')
        self.info.setText('')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        target = self.view.size() - QSize(6, 6)
        if target.width() <= 0 or target.height() <= 0:
            return
        self.view.setPixmap(self._pixmap.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


# ==================================================================== 辅助

def configure_table_view(view, model: RecordTableModel,
                         proxy=None, row_height: int = 26) -> None:
    """统一的表格外观设置。"""
    from PySide6.QtWidgets import QAbstractItemView, QHeaderView

    view.setModel(proxy if proxy is not None else model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                         | QAbstractItemView.EditTrigger.EditKeyPressed)
    view.setAlternatingRowColors(False)
    view.setSortingEnabled(True)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(row_height)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for i, column in enumerate(model.columns):
        view.setColumnWidth(i, column.width)


def selected_record(view, model: RecordTableModel, proxy=None) -> ImageRecord | None:
    """取当前选中行对应的记录。"""
    index = view.currentIndex()
    if not index.isValid():
        return None
    if proxy is not None:
        index = proxy.mapToSource(index)
    return model.record_at(index.row()) if hasattr(model, 'record_at') else None


def make_hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName('hint')
    label.setWordWrap(True)
    return label


def make_separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet('color:#e3e7eb;')
    return line


def open_path(path: str) -> None:
    """在资源管理器中打开。"""
    from utils import file_utils
    if path:
        file_utils.open_in_explorer(path)

"""
第一步：设置任务（根目录 / 路径规则 / 输出目录 / 命名规则）。

界面上的任何操作都是只读的：读取目录名、预览规则，绝不改动文件。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout,
                               QWidget)

from core.path_matcher import (build_template, describe_template_effect,
                               normalize_template, validate_template)
from models import ExtractRule, NamingConfig, OcrConfig, OutputConfig, TaskConfig
from ui.widgets import make_hint, make_separator
from utils import file_utils
from utils.logger import get_logger

logger = get_logger('ui.settings')

ALL_FOLDERS = '所有文件夹（*）'
COMMON_FIELDS = ['姓名', '编号', '公司名称', '产品名称', '日期', '合同编号',
                 '电话', '地址', '班级']

PREPROCESS_ITEMS = [
    ('standard', '标准（推荐：增强对比 + 降噪）'),
    ('light', '轻度（缩放 + 灰度）'),
    ('strong', '强力（纠偏 + 锐化 + 二值化）'),
    ('none', '不处理（最快）'),
]


class SettingsPage(QWidget):
    """任务设置页。"""

    scan_requested = Signal()
    config_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = TaskConfig()
        self._build()
        # 引擎探测需要 import onnxruntime 等重模块，放到界面显示之后再做，
        # 避免拖慢启动（首屏立刻可见）
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.refresh_engine_status)

    # ================================================================ 构建界面

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 8, 16, 16)

        title = QLabel('第一步 · 设置提取任务')
        title.setObjectName('title')
        outer.addWidget(title)
        outer.addWidget(make_hint(
            '先在下方选择根目录并设置路径规则，然后点击"扫描文件"。'
            '扫描阶段只读取文件信息，不会复制、移动或修改任何文件。'))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 8, 8, 8)
        layout.setSpacing(12)

        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_output_group())
        layout.addWidget(self._build_naming_group())
        layout.addWidget(self._build_ocr_group())
        layout.addStretch(1)

        # 底部按钮
        bottom = QHBoxLayout()
        self.preview_label = QLabel('')
        self.preview_label.setObjectName('hint')
        bottom.addWidget(self.preview_label, 1)
        self.scan_button = QPushButton('扫描文件')
        self.scan_button.setObjectName('primary')
        self.scan_button.setMinimumWidth(140)
        self.scan_button.clicked.connect(self._on_scan_clicked)
        bottom.addWidget(self.scan_button)
        outer.addLayout(bottom)

    # ---------------------------------------------------------------- 来源

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox('1. 从哪里找（根目录与路径规则）')
        layout = QVBoxLayout(group)

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel('根目录：'))
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText(r'例如 D:\示例数据\20250101')
        self.root_edit.textChanged.connect(self._on_root_changed)
        root_row.addWidget(self.root_edit, 1)
        root_button = QPushButton('选择文件夹…')
        root_button.clicked.connect(self._pick_root)
        root_row.addWidget(root_button)
        self.read_dirs_button = QPushButton('读取目录结构')
        self.read_dirs_button.clicked.connect(self._read_dirs)
        root_row.addWidget(self.read_dirs_button)
        layout.addLayout(root_row)

        # 模式选择
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel('提取方式：'))
        self.radio_basic = QRadioButton('普通模式（下拉选择）')
        self.radio_advanced = QRadioButton('高级模式（直接写路径模板）')
        self.radio_basic.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.radio_basic, 0)
        self.mode_group.addButton(self.radio_advanced, 1)
        self.radio_basic.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.radio_basic)
        mode_row.addWidget(self.radio_advanced)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # 两种模式的堆叠
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_basic_panel())
        self.mode_stack.addWidget(self._build_advanced_panel())
        layout.addWidget(self.mode_stack)

        # 规则预览
        self.rule_preview = QLabel('')
        self.rule_preview.setWordWrap(True)
        self.rule_preview.setStyleSheet(
            'background:#f2f7fc; border:1px solid #cfe0f0; border-radius:4px; '
            'padding:8px; color:#245a91;')
        layout.addWidget(self.rule_preview)
        return group

    def _build_basic_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(6, 6, 6, 6)

        self.level1_combo = self._folder_combo()
        self.level2_combo = self._folder_combo()
        self.file_combo = QComboBox()
        self.file_combo.setEditable(True)
        self.file_combo.setMinimumWidth(320)
        self.file_combo.currentTextChanged.connect(self._refresh_rule_preview)

        form.addRow('第一层文件夹：', self.level1_combo)
        form.addRow('第二层文件夹：', self.level2_combo)
        form.addRow('目标图片：', self.file_combo)
        form.addRow('', make_hint(
            '选择"所有文件夹"代表该层可以是任意目录名（对应模板中的 *）。'
            '点击"读取目录结构"可以从根目录读取真实存在的文件夹名，方便直接挑选。'))
        return panel

    def _folder_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setMinimumWidth(320)
        combo.addItem(ALL_FOLDERS, '*')
        combo.currentTextChanged.connect(self._refresh_rule_preview)
        return combo

    def _build_advanced_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText('例如：*/1440/SAMPLE_NA.jpg')
        self.template_edit.textChanged.connect(self._refresh_rule_preview)
        layout.addWidget(self.template_edit)

        layout.addWidget(make_hint(
            '模板说明：* 匹配"恰好一层"目录名；** 匹配任意层级目录（范围更大，慎用）；\n'
            '最后一段是文件名（可以使用通配符，例如 *.jpg）。路径分隔符用 / 或 \\ 都可以。'))

        example_row = QHBoxLayout()
        for text in ('*/1440/SAMPLE_NA.jpg',
                     '*/2880/SAMPLE_NA.jpg',
                     '**/1440/*.jpg'):
            button = QPushButton(text)
            button.setObjectName('ghost')
            button.clicked.connect(lambda _=False, t=text: self.template_edit.setText(t))
            example_row.addWidget(button)
        example_row.addStretch(1)
        layout.addLayout(example_row)
        return panel

    # ---------------------------------------------------------------- 输出

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox('2. 复制到哪里（输出目录）')
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel('输出目录：'))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(r'例如 D:\整理结果')
        self.output_edit.textChanged.connect(self._refresh_rule_preview)
        row.addWidget(self.output_edit, 1)
        output_button = QPushButton('选择文件夹…')
        output_button.clicked.connect(self._pick_output)
        row.addWidget(output_button)
        layout.addLayout(row)

        policy_row = QHBoxLayout()
        policy_row.addWidget(QLabel('重名处理：'))
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem('自动改名（追加 -1 / -2 / -3）', 'rename')
        self.conflict_combo.addItem('跳过该文件', 'skip')
        policy_row.addWidget(self.conflict_combo)

        self.keep_structure_check = QCheckBox('保留原始层级结构（在输出目录下重建子文件夹）')
        policy_row.addWidget(self.keep_structure_check)
        policy_row.addStretch(1)
        layout.addLayout(policy_row)

        report_row = QHBoxLayout()
        self.report_check = QCheckBox('任务结束后生成处理报告')
        self.report_check.setChecked(True)
        report_row.addWidget(self.report_check)
        self.report_csv_check = QCheckBox('CSV')
        self.report_csv_check.setChecked(True)
        self.report_json_check = QCheckBox('JSON')
        self.report_json_check.setChecked(True)
        self.report_excel_check = QCheckBox('Excel（需要 openpyxl）')
        for check in (self.report_csv_check, self.report_json_check,
                      self.report_excel_check):
            report_row.addWidget(check)
        report_row.addStretch(1)
        layout.addLayout(report_row)

        layout.addWidget(make_hint(
            '安全说明：程序只做"复制"，原始文件永远保持原样；'
            '目标目录中已有的同名文件永远不会被覆盖。'))
        return group

    # ---------------------------------------------------------------- 命名

    def _build_naming_group(self) -> QGroupBox:
        group = QGroupBox('3. 怎么命名（OCR 字段与来源信息）')
        layout = QVBoxLayout(group)

        field_row = QHBoxLayout()
        field_row.addWidget(QLabel('命名字段：'))
        self.fields_edit = QLineEdit('姓名')
        self.fields_edit.setPlaceholderText('多个字段用逗号分隔，按顺序优先匹配，例如：姓名,客户名称')
        field_row.addWidget(self.fields_edit, 1)
        layout.addLayout(field_row)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel('常用字段：'))
        for field in COMMON_FIELDS:
            button = QPushButton(field)
            button.setObjectName('ghost')
            button.clicked.connect(lambda _=False, f=field: self._append_field(f))
            quick_row.addWidget(button)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)

        layout.addWidget(make_hint(
            '命名优先级：① 指定字段命中 → ② 本地 AI 判断 → ③ 人工确认。'
            '字段全部失败时不会随意取 OCR 第一行。'))

        layout.addWidget(make_separator())

        source_row = QHBoxLayout()
        self.keep_source_check = QCheckBox('保留来源文件夹信息')
        self.keep_source_check.setChecked(True)
        self.keep_source_check.toggled.connect(self._refresh_rule_preview)
        source_row.addWidget(self.keep_source_check)

        source_row.addWidget(QLabel('位置：'))
        self.source_pos_combo = QComboBox()
        self.source_pos_combo.addItem('前缀：CASE001_张三.jpg', 'prefix')
        self.source_pos_combo.addItem('后缀：张三_CASE001.jpg', 'suffix')
        self.source_pos_combo.currentIndexChanged.connect(self._refresh_rule_preview)
        source_row.addWidget(self.source_pos_combo)

        source_row.addWidget(QLabel('连接符：'))
        self.separator_edit = QLineEdit('_')
        self.separator_edit.setMaximumWidth(50)
        self.separator_edit.textChanged.connect(self._refresh_rule_preview)
        source_row.addWidget(self.separator_edit)

        source_row.addWidget(QLabel('来源层级：'))
        self.source_level_combo = QComboBox()
        self.source_level_combo.addItem('第 1 层（默认）', 0)
        self.source_level_combo.addItem('第 2 层', 1)
        self.source_level_combo.addItem('第 3 层', 2)
        source_row.addWidget(self.source_level_combo)
        source_row.addStretch(1)
        layout.addLayout(source_row)

        # AI 选项
        ai_row = QHBoxLayout()
        self.ai_check = QCheckBox('字段失败时启用本地 AI 判断')
        self.ai_check.setChecked(True)
        ai_row.addWidget(self.ai_check)
        ai_row.addWidget(QLabel('AI 置信度阈值：'))
        self.ai_threshold_combo = QComboBox()
        for value, label in ((0.45, '宽松 0.45'), (0.60, '标准 0.60'), (0.75, '严格 0.75')):
            self.ai_threshold_combo.addItem(label, value)
        self.ai_threshold_combo.setCurrentIndex(1)
        ai_row.addWidget(self.ai_threshold_combo)
        ai_row.addStretch(1)
        layout.addLayout(ai_row)

        ollama_row = QHBoxLayout()
        self.ollama_check = QCheckBox('使用本地大模型（Ollama，可选）')
        self.ollama_check.toggled.connect(self._on_ollama_toggled)
        ollama_row.addWidget(self.ollama_check)
        self.ollama_url_edit = QLineEdit('http://127.0.0.1:11434')
        self.ollama_url_edit.setMaximumWidth(200)
        self.ollama_url_edit.setEnabled(False)
        ollama_row.addWidget(self.ollama_url_edit)
        self.ollama_model_edit = QLineEdit('qwen2.5:7b')
        self.ollama_model_edit.setMaximumWidth(140)
        self.ollama_model_edit.setEnabled(False)
        ollama_row.addWidget(self.ollama_model_edit)
        ollama_row.addStretch(1)
        layout.addLayout(ollama_row)
        layout.addWidget(make_hint(
            '本程序默认完全离线：不上传图片、不上传 OCR 文本。'
            '本地大模型只能填写 127.0.0.1 / localhost 地址，从机制上不可能外传数据。'))

        fallback_row = QHBoxLayout()
        self.fallback_source_check = QCheckBox('无法识别时用"来源文件夹名"兜底命名')
        self.fallback_source_check.setChecked(True)
        self.fallback_original_check = QCheckBox('用原文件名兜底')
        fallback_row.addWidget(self.fallback_source_check)
        fallback_row.addWidget(self.fallback_original_check)
        fallback_row.addStretch(1)
        layout.addLayout(fallback_row)
        return group

    # ---------------------------------------------------------------- OCR

    def _build_ocr_group(self) -> QGroupBox:
        group = QGroupBox('4. OCR 设置')
        layout = QGridLayout(group)

        layout.addWidget(QLabel('识别引擎：'), 0, 0)
        self.engine_combo = QComboBox()
        self.engine_combo.addItem('自动选择（推荐）', 'auto')
        self.engine_combo.addItem('PaddleOCR（最准，需自行安装）', 'paddle')
        self.engine_combo.addItem('RapidOCR（轻量，ONNXRuntime）', 'rapid')
        self.engine_combo.addItem('不使用 OCR（仅按来源命名）', 'none')
        layout.addWidget(self.engine_combo, 0, 1)

        self.engine_status_label = QLabel('')
        self.engine_status_label.setObjectName('hint')
        layout.addWidget(self.engine_status_label, 0, 2, 1, 2)

        layout.addWidget(QLabel('图像预处理：'), 1, 0)
        self.preprocess_combo = QComboBox()
        for value, label in PREPROCESS_ITEMS:
            self.preprocess_combo.addItem(label, value)
        layout.addWidget(self.preprocess_combo, 1, 1)
        layout.addWidget(make_hint(
            '预处理只在内存中进行，用于提升识别率，不会改动原始图片。'), 2, 1, 1, 3)

        self.recheck_button = QPushButton('检测 OCR 环境')
        self.recheck_button.clicked.connect(self.refresh_engine_status)
        layout.addWidget(self.recheck_button, 2, 0)
        return group

    # ================================================================ 交互

    def _on_mode_changed(self) -> None:
        self.mode_stack.setCurrentIndex(1 if self.radio_advanced.isChecked() else 0)
        self._refresh_rule_preview()

    def _on_root_changed(self) -> None:
        self._refresh_rule_preview()
        self.config_changed.emit()

    def _on_ollama_toggled(self, checked: bool) -> None:
        self.ollama_url_edit.setEnabled(checked)
        self.ollama_model_edit.setEnabled(checked)
        if checked:
            # 只做本地地址校验（不联网探测，避免阻塞界面）；服务是否在线
            # 由 ocr/suggester.py 在真正调用时判断，失败会自动回退到内置 AI
            from ocr.suggester import OllamaSuggester
            client = OllamaSuggester(self.ollama_url_edit.text(),
                                     self.ollama_model_edit.text())
            if not client.is_local():
                self.ollama_url_edit.setText('http://127.0.0.1:11434')

    def _append_field(self, field: str) -> None:
        current = [f.strip() for f in self.fields_edit.text().split(',') if f.strip()]
        if field not in current:
            current.append(field)
        self.fields_edit.setText(', '.join(current))

    def _pick_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, '选择根目录（原始数据所在的大文件夹）', self.root_edit.text() or '')
        if path:
            self.root_edit.setText(os.path.normpath(path))
            self._read_dirs(auto=True)

    def _pick_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, '选择输出目录（整理结果保存位置）', self.output_edit.text() or '')
        if path:
            self.output_edit.setText(os.path.normpath(path))

    # ---------------------------------------------------------------- 读取目录

    def _read_dirs(self, auto: bool = False) -> None:
        """从根目录读取真实存在的文件夹/文件名，填充下拉框（只读操作）。"""
        root = self.root_edit.text().strip()
        if not root or not os.path.isdir(root):
            if not auto:
                self._set_preview('请先选择有效的根目录，然后再读取目录结构。')
            return

        level1_names = file_utils.list_subdir_names(root)
        current1 = self._combo_value(self.level1_combo)
        self.level1_combo.blockSignals(True)
        self.level1_combo.clear()
        self.level1_combo.addItem(ALL_FOLDERS, '*')
        for name in level1_names:
            self.level1_combo.addItem(name, name)
        self._restore_combo(self.level1_combo, current1)
        self.level1_combo.blockSignals(False)

        # 第二层：抽取前若干个一级目录的公共子目录
        probe_roots = [os.path.join(root, n) for n in level1_names[:20]]
        level2_names: list[str] = []
        seen = set()
        for probe in probe_roots:
            for name in file_utils.list_subdir_names(probe):
                if name not in seen:
                    seen.add(name)
                    level2_names.append(name)
        current2 = self._combo_value(self.level2_combo)
        self.level2_combo.blockSignals(True)
        self.level2_combo.clear()
        self.level2_combo.addItem(ALL_FOLDERS, '*')
        for name in level2_names:
            self.level2_combo.addItem(name, name)
        self._restore_combo(self.level2_combo, current2)
        self.level2_combo.blockSignals(False)

        # 文件名：从第一个可用的 <一级>/<二级> 目录里取
        file_names: list[str] = []
        target1 = self._combo_value(self.level1_combo)
        target2 = self._combo_value(self.level2_combo)
        if target1 in ('', '*'):
            target1 = level1_names[0] if level1_names else ''
        if target2 in ('', '*'):
            target2 = self.level2_combo.itemText(1) if self.level2_combo.count() > 1 else ''
        if target1:
            probe_dir = os.path.join(root, target1)
            if target2 and target2 != '*':
                probe_dir = os.path.join(probe_dir, target2)
            file_names = file_utils.list_file_names(
                probe_dir, file_utils.IMAGE_EXTENSIONS)

        current3 = self.file_combo.currentText()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        for name in file_names:
            self.file_combo.addItem(name)
        if current3 and self.file_combo.findText(current3) < 0:
            self.file_combo.addItem(current3)
        if current3:
            self.file_combo.setCurrentText(current3)
        elif file_names:
            self.file_combo.setCurrentIndex(0)
        self.file_combo.blockSignals(False)

        self._refresh_rule_preview()

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        data = combo.currentData()
        if data:
            return str(data)
        text = combo.currentText().strip()
        if not text or text == ALL_FOLDERS:
            return '*'
        return text

    @staticmethod
    def _restore_combo(combo: QComboBox, value: str) -> None:
        if not value:
            return
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif value != '*':
            combo.setCurrentText(value)

    # ---------------------------------------------------------------- 规则预览

    def _current_template(self) -> str:
        if self.radio_advanced.isChecked():
            return normalize_template(self.template_edit.text())
        return build_template(self._combo_value(self.level1_combo),
                              self._combo_value(self.level2_combo),
                              self.file_combo.currentText(), 'basic')

    def _refresh_rule_preview(self) -> None:
        """实时显示将要使用的完整模板与含义。"""
        template = self._current_template()
        if not template:
            self.rule_preview.setText('匹配规则：（尚未设置）')
            return
        ok, message = validate_template(template)
        root = self.root_edit.text().strip()

        lines = [f'<b>匹配规则：</b>{template}']
        if root:
            parts = template.split('/')
            source_hint = parts[0] if parts else ''
            lines.append(f'<b>来源文件夹：</b>'
                         f'{source_hint if source_hint not in ("*", "**") else "第一层文件夹名"}')
            lines.append(describe_template_effect(template, os.path.basename(root)))
        if not ok:
            lines.append(f'<span style="color:#c0392b">⚠ {message}</span>')
        elif message:
            lines.append(f'<span style="color:#6b7280">{message}</span>')

        # 文件名示例
        naming = self._collect_naming()
        sample_source = 'CASE001'
        sample_name = '张三'
        lines.append(f'<b>命名示例：</b>{self._sample_filename(naming, sample_source, sample_name)}')

        self.rule_preview.setText('<br>'.join(lines))
        self._update_scan_button()

    def _sample_filename(self, naming: NamingConfig, source: str, name: str) -> str:
        from core.filename_manager import compose_base_stem
        return compose_base_stem(name, source, naming) + '.jpg'

    def _update_scan_button(self) -> None:
        template = self._current_template()
        ok, _ = validate_template(template) if template else (False, '')
        has_root = bool(self.root_edit.text().strip())
        self.scan_button.setEnabled(ok and has_root)

    def _set_preview(self, text: str) -> None:
        self.rule_preview.setText(text)

    # ================================================================ 引擎状态

    def refresh_engine_status(self) -> None:
        """检测并展示各 OCR 引擎的可用性。"""
        try:
            from ocr.engine import engine_status
            status = engine_status()
        except Exception as exc:
            self.engine_status_label.setText(f'OCR 环境检测失败：{exc}')
            return
        parts = []
        for key, info in status.items():
            mark = '✔' if info.get('available') else '✘'
            parts.append(f'{mark} {info.get("name", key)}')
        text = '　'.join(parts)
        if not any(info.get('available') for info in status.values()):
            text += '　→ 暂不可用，可先只做提取，稍后再装 OCR'
        self.engine_status_label.setText(text)

    # ================================================================ 配置读写

    def load_config(self, config: TaskConfig) -> None:
        """把配置写入界面。"""
        self.config = config
        rule = config.rule
        self.root_edit.setText(rule.root_dir or '')
        self.output_edit.setText(config.output.output_dir or '')
        self.template_edit.setText(rule.template or '')
        if rule.mode == 'advanced':
            self.radio_advanced.setChecked(True)
        else:
            self.radio_basic.setChecked(True)
        self.mode_stack.setCurrentIndex(1 if rule.mode == 'advanced' else 0)

        self._restore_combo(self.level1_combo, rule.level1 or '*')
        self._restore_combo(self.level2_combo, rule.level2 or '*')
        if rule.file_name:
            self.file_combo.blockSignals(True)
            self.file_combo.setCurrentText(rule.file_name)
            self.file_combo.blockSignals(False)

        naming = config.naming
        self.fields_edit.setText(', '.join(naming.fields or ['姓名']))
        self.keep_source_check.setChecked(naming.keep_source)
        index = self.source_pos_combo.findData(naming.source_position)
        self.source_pos_combo.setCurrentIndex(max(0, index))
        self.separator_edit.setText(naming.separator or '_')
        self.source_level_combo.setCurrentIndex(
            max(0, min(2, int(naming.source_level or 0))))
        self.ai_check.setChecked(naming.ai_enabled)
        threshold_index = self.ai_threshold_combo.findData(round(
            float(getattr(naming, 'ai_min_confidence', 0.6)), 2))
        self.ai_threshold_combo.setCurrentIndex(max(0, threshold_index))
        self.ollama_check.setChecked(bool(naming.ollama_enabled))
        self.ollama_url_edit.setText(naming.ollama_url)
        self.ollama_model_edit.setText(naming.ollama_model)
        self._on_ollama_toggled(bool(naming.ollama_enabled))
        self.fallback_source_check.setChecked(naming.fallback_to_source)
        self.fallback_original_check.setChecked(naming.fallback_to_original)

        output = config.output
        index = self.conflict_combo.findData(output.conflict_policy)
        self.conflict_combo.setCurrentIndex(max(0, index))
        self.keep_structure_check.setChecked(output.keep_structure)
        self.report_check.setChecked(output.write_report)
        formats = [f.lower() for f in (output.report_formats or [])]
        self.report_csv_check.setChecked('csv' in formats)
        self.report_json_check.setChecked('json' in formats)
        self.report_excel_check.setChecked('excel' in formats or 'xlsx' in formats)

        ocr_conf = config.ocr
        index = self.engine_combo.findData(ocr_conf.engine)
        self.engine_combo.setCurrentIndex(max(0, index))
        index = self.preprocess_combo.findData(ocr_conf.preprocess)
        self.preprocess_combo.setCurrentIndex(max(0, index))

        self._refresh_rule_preview()

    def _collect_naming(self) -> NamingConfig:
        fields = [f.strip() for f in self.fields_edit.text().replace('，', ',').split(',')
                  if f.strip()]
        return NamingConfig(
            fields=fields or ['姓名'],
            keep_source=self.keep_source_check.isChecked(),
            source_position=self.source_pos_combo.currentData() or 'prefix',
            separator=(self.separator_edit.text() or '_'),
            source_level=int(self.source_level_combo.currentData() or 0),
            ai_enabled=self.ai_check.isChecked(),
            ai_min_confidence=float(self.ai_threshold_combo.currentData() or 0.6),
            ollama_enabled=self.ollama_check.isChecked(),
            ollama_url=self.ollama_url_edit.text().strip() or 'http://127.0.0.1:11434',
            ollama_model=self.ollama_model_edit.text().strip() or 'qwen2.5:7b',
            fallback_to_source=self.fallback_source_check.isChecked(),
            fallback_to_original=self.fallback_original_check.isChecked(),
        )

    def collect(self) -> TaskConfig:
        """从界面收集配置；不合法时抛 ValueError。"""
        root = self.root_edit.text().strip()
        if not root:
            raise ValueError('请先选择根目录')
        if not os.path.isdir(root):
            raise ValueError(f'根目录不存在或不是文件夹：\n{root}')

        template = self._current_template()
        ok, message = validate_template(template)
        if not ok:
            raise ValueError(f'路径规则无效：{message}')

        output_dir = self.output_edit.text().strip()
        if not output_dir:
            raise ValueError('请先选择输出目录')
        if os.path.abspath(output_dir) == os.path.abspath(root):
            raise ValueError('输出目录不能与根目录相同，否则会污染原始数据')

        formats = []
        if self.report_csv_check.isChecked():
            formats.append('csv')
        if self.report_json_check.isChecked():
            formats.append('json')
        if self.report_excel_check.isChecked():
            formats.append('excel')

        config = TaskConfig(
            rule=ExtractRule(
                root_dir=root,
                mode='advanced' if self.radio_advanced.isChecked() else 'basic',
                template=template,
                level1=self._combo_value(self.level1_combo),
                level2=self._combo_value(self.level2_combo),
                file_name=self.file_combo.currentText().strip(),
            ),
            naming=self._collect_naming(),
            ocr=OcrConfig(
                engine=self.engine_combo.currentData() or 'auto',
                preprocess=self.preprocess_combo.currentData() or 'standard',
            ),
            output=OutputConfig(
                output_dir=output_dir,
                conflict_policy=self.conflict_combo.currentData() or 'rename',
                keep_structure=self.keep_structure_check.isChecked(),
                write_report=self.report_check.isChecked(),
                report_formats=formats,
            ),
        )
        self.config = config
        return config

    def _on_scan_clicked(self) -> None:
        try:
            self.collect()
        except ValueError as exc:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, '请先完善设置', str(exc))
            return
        self.scan_requested.emit()

    def set_busy(self, busy: bool) -> None:
        self.scan_button.setEnabled(not busy)
        self.read_dirs_button.setEnabled(not busy)
        self.scan_button.setText('正在扫描…' if busy else '扫描文件')

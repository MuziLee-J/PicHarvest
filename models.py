"""
全局数据结构定义（纯标准库）。

所有模块共享这些 dataclass，UI 与 core 之间通过它们传递数据，
从而保证 core 层不依赖任何 GUI 类型。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ==================================================================== 状态常量

class Status:
    """单个文件的处理状态。"""
    PENDING = 'pending'              # 已扫描，未 OCR
    OCR_DONE = 'ocr_done'            # OCR 完成并已生成建议名
    NEEDS_REVIEW = 'needs_review'    # 需人工确认
    READY = 'ready'                  # 就绪（可执行）
    SKIPPED = 'skipped'             # 用户跳过
    SUCCESS = 'success'             # 已成功复制
    FAILED = 'failed'              # 处理失败
    CANCELED = 'canceled'          # 用户取消，未处理

    LABELS = {
        PENDING: '待处理',
        OCR_DONE: '已识别',
        NEEDS_REVIEW: '需人工确认',
        READY: '就绪',
        SKIPPED: '已跳过',
        SUCCESS: '成功',
        FAILED: '失败',
        CANCELED: '已取消',
    }

    @classmethod
    def label(cls, status: str) -> str:
        return cls.LABELS.get(status, status or '未知')


class ExtractMethod:
    """名字提取方式。"""
    FIELD = 'field'        # 用户指定字段
    AI = 'ai'              # 本地 AI 判断
    MANUAL = 'manual'      # 人工输入
    FALLBACK = 'fallback'  # 兜底（来源名 / 原文件名）

    LABELS = {
        FIELD: '指定字段',
        AI: 'AI判断',
        MANUAL: '人工修改',
        FALLBACK: '兜底',
    }


class TaskState:
    """任务整体状态。"""
    CREATED = 'created'
    SCANNED = 'scanned'
    OCR_RUNNING = 'ocr_running'
    READY = 'ready'
    RUNNING = 'running'
    PAUSED = 'paused'
    FINISHED = 'finished'
    CANCELED = 'canceled'
    FAILED = 'failed'


# ==================================================================== 规则 / 配置

@dataclass
class ExtractRule:
    """提取规则：根目录 + 路径模板。"""
    root_dir: str = ''
    mode: str = 'basic'                      # 'basic' 普通模式 | 'advanced' 高级模式
    template: str = '*/1440/SAMPLE_NA.jpg'
    level1: str = '*'                        # 普通模式：第一层（'*' 表示所有文件夹）
    level2: str = '1440'                     # 普通模式：第二层
    file_name: str = 'SAMPLE_NA.jpg'  # 普通模式：目标图片名

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'ExtractRule':
        return cls(**(data or {}))


@dataclass
class NamingConfig:
    """命名规则。"""
    fields: list = field(default_factory=lambda: ['姓名'])  # 用户指定字段（按优先级）
    keep_source: bool = True                 # 是否保留来源文件夹信息
    source_position: str = 'prefix'          # 'prefix' 前缀 | 'suffix' 后缀
    separator: str = '_'                     # 连接符
    source_level: int = 0                    # 来源文件夹在相对路径中的层号（0 = 第一层）
    ai_enabled: bool = True                  # 字段失败时启用 AI 判断
    ai_min_confidence: float = 0.60          # AI 结果低于该置信度 -> 转人工确认
    ollama_enabled: bool = False             # 可选：本地 Ollama 大模型
    ollama_url: str = 'http://127.0.0.1:11434'
    ollama_model: str = 'qwen2.5:7b'
    max_stem_len: int = 100
    fallback_to_source: bool = True          # 全部失败时用"来源文件夹"兜底命名
    fallback_to_original: bool = False       # 兜底用原文件名

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'NamingConfig':
        base = cls()
        for key, value in (data or {}).items():
            if hasattr(base, key):
                setattr(base, key, value)
        return base


@dataclass
class OcrConfig:
    """OCR 配置。"""
    engine: str = 'auto'             # auto | paddle | rapid | none
    lang: str = 'ch'
    preprocess: str = 'standard'     # none | light | standard | strong
    min_confidence: float = 0.45     # 低于该值视为不可靠
    timeout_sec: int = 60            # 单张图 OCR 超时
    max_side: int = 2200             # 预处理时最长边上限（控制内存）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'OcrConfig':
        base = cls()
        for key, value in (data or {}).items():
            if hasattr(base, key):
                setattr(base, key, value)
        return base


@dataclass
class OutputConfig:
    """输出配置。"""
    output_dir: str = ''
    conflict_policy: str = 'rename'   # rename（追加 -1）| skip（跳过）
    keep_structure: bool = False      # 保留原层级（本版默认扁平化）
    write_report: bool = True
    report_formats: list = field(default_factory=lambda: ['csv', 'json'])
    verify_copy: bool = True          # 复制后校验文件大小

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'OutputConfig':
        base = cls()
        for key, value in (data or {}).items():
            if hasattr(base, key):
                setattr(base, key, value)
        return base


@dataclass
class TaskConfig:
    """完整任务配置。"""
    rule: ExtractRule = field(default_factory=ExtractRule)
    naming: NamingConfig = field(default_factory=NamingConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    filter_extensions: list = field(default_factory=list)   # 额外的扩展名白名单，空=按模板
    max_depth: int = 32                                     # 递归深度上限（防跑飞）

    def to_dict(self) -> dict:
        return {
            'rule': self.rule.to_dict(),
            'naming': self.naming.to_dict(),
            'ocr': self.ocr.to_dict(),
            'output': self.output.to_dict(),
            'filter_extensions': list(self.filter_extensions),
            'max_depth': self.max_depth,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'TaskConfig':
        data = data or {}
        return cls(
            rule=ExtractRule.from_dict(data.get('rule')),
            naming=NamingConfig.from_dict(data.get('naming')),
            ocr=OcrConfig.from_dict(data.get('ocr')),
            output=OutputConfig.from_dict(data.get('output')),
            filter_extensions=list(data.get('filter_extensions') or []),
            max_depth=int(data.get('max_depth') or 32),
        )


# ==================================================================== 文件记录

@dataclass
class ImageRecord:
    """单个待处理图片的完整生命周期数据（对应需求书第三十二节）。"""

    # --- 扫描阶段（只读）
    index: int = 0
    source_path: str = ''
    rel_path: str = ''                 # 相对根目录，统一用 / 分隔
    source_folder: str = ''            # 来源文件夹（默认第一层）
    original_name: str = ''
    ext: str = ''
    size: int = 0
    mtime: float = 0.0

    # --- OCR 阶段（只读）
    ocr_text: str = ''
    ocr_lines: list = field(default_factory=list)
    ocr_engine: str = ''
    ocr_confidence: float = 0.0
    ocr_error: Optional[str] = None
    ocr_done: bool = False

    # --- 命名阶段
    extracted_name: str = ''           # 纯名称（不含来源前缀）
    extract_method: str = ''           # Status / ExtractMethod
    extract_reason: str = ''           # 说明（调试/展示用）
    suggested_filename: str = ''       # 系统建议的完整文件名
    final_filename: str = ''           # 最终文件名（用户可改）
    manual_override: bool = False      # 用户是否手工改过

    # --- 执行阶段
    status: str = Status.PENDING
    error: Optional[str] = None
    conflict: bool = False             # 是否发生了重名改名
    conflict_note: str = ''
    dest_path: str = ''                # 实际写入路径
    created: bool = False              # 是否由本任务新建（撤销依据）

    # ------------------------------------------------------------------

    @property
    def selected(self) -> bool:
        return self.status not in (Status.SKIPPED, Status.SUCCESS, Status.FAILED, Status.CANCELED)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'ImageRecord':
        base = cls()
        for key, value in (data or {}).items():
            if hasattr(base, key):
                setattr(base, key, value)
        return base

    def dest_dir(self, output_dir: str, keep_structure: bool = False) -> str:
        """计算目标目录。"""
        import os
        if not keep_structure:
            return output_dir
        parent_rel = os.path.dirname(self.rel_path)
        return os.path.join(output_dir, parent_rel.replace('/', os.sep)) if parent_rel else output_dir

    def to_db_tuple(self, task_id: int) -> tuple:
        return (
            task_id, self.index, self.source_path, self.rel_path, self.source_folder,
            self.original_name, self.size, self.mtime, self.ocr_text, self.ocr_engine,
            self.ocr_confidence, self.ocr_error, self.extracted_name, self.extract_method,
            self.extract_reason, self.suggested_filename, self.final_filename,
            self.status, self.error, self.conflict, self.conflict_note, self.dest_path,
            1 if self.created else 0,
        )


# ==================================================================== 任务与进度

@dataclass
class TaskSummary:
    """任务级统计。"""
    total: int = 0
    selected: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    canceled: int = 0
    ocr_ok: int = 0
    ocr_failed: int = 0
    needs_review: int = 0
    conflicts: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def elapsed_text(self) -> str:
        secs = int(self.elapsed)
        if secs < 60:
            return f'{secs} 秒'
        if secs < 3600:
            return f'{secs // 60} 分 {secs % 60} 秒'
        return f'{secs // 3600} 小时 {(secs % 3600) // 60} 分'

    def to_dict(self) -> dict:
        data = asdict(self)
        data['elapsed'] = round(self.elapsed, 2)
        data['elapsed_text'] = self.elapsed_text
        return data

    @classmethod
    def from_records(cls, records: list, **extra) -> 'TaskSummary':
        summary = cls(**extra)
        summary.total = len(records)
        for rec in records:
            if rec.status == Status.SUCCESS:
                summary.success += 1
            elif rec.status == Status.FAILED:
                summary.failed += 1
            elif rec.status == Status.SKIPPED:
                summary.skipped += 1
            elif rec.status == Status.CANCELED:
                summary.canceled += 1
            if rec.ocr_done:
                summary.ocr_ok += 1
            elif rec.ocr_error:
                summary.ocr_failed += 1
            if rec.status == Status.NEEDS_REVIEW:
                summary.needs_review += 1
            if rec.conflict:
                summary.conflicts += 1
        return summary


@dataclass
class ProgressEvent:
    """进度回调统一结构。"""
    phase: str = ''            # scan | ocr | execute | undo
    current: int = 0
    total: int = 0
    message: str = ''
    source: str = ''
    dest: str = ''
    ocr_text: str = ''
    status: str = ''
    success: int = 0
    failed: int = 0
    skipped: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return int(self.current * 100 / self.total)


@dataclass
class ScanResult:
    """扫描结果。"""
    root_dir: str = ''
    template: str = ''
    files: list = field(default_factory=list)
    scanned_dirs: int = 0
    elapsed: float = 0.0
    errors: list = field(default_factory=list)   # [(path, error)]
    matched_all: int = 0

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass
class ExecutionResult:
    """执行结果。"""
    summary: TaskSummary = field(default_factory=TaskSummary)
    errors: list = field(default_factory=list)
    report_paths: list = field(default_factory=list)


def now_text() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')

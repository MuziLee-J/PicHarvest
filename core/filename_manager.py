"""
文件名拼接与重名分配（纯标准库）。

职责：
1. 把「纯名称 + 来源文件夹」拼成合法的最终文件名；
2. 全局唯一分配器：同时考虑
   - 输出目录中已存在的文件
   - 本次任务中已经分配过的名字
   - Windows 大小写不敏感
3. 冲突时依次生成 -1 / -2 / -3 ...，绝不覆盖。

注意：分配器只做"预分配"，真正落盘时 core/extractor.py 还会用
os.O_EXCL 独占创建做最后一道防线。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from models import ImageRecord, NamingConfig, OutputConfig, Status
from utils import file_utils
from utils.validators import fit_path_length, sanitize_stem, split_extension

#: 冲突后缀与主名之间的连接符
CONFLICT_SUFFIX_SEP = '-'
#: 单个目录最多尝试的冲突次数
MAX_CONFLICT_TRY = 10000


@dataclass
class AllocationResult:
    """一次分配的結果。"""
    filename: str = ''
    conflict: bool = False
    note: str = ''
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.filename) and not self.skipped


# ---------------------------------------------------------------- 名称拼接

def sanitize_pure_name(name: str, naming: NamingConfig) -> str:
    """清洗 OCR 得到的纯名称（此时还不含来源前缀）。"""
    return sanitize_stem(
        name,
        replacement=naming.separator or '_',
        max_len=max(20, naming.max_stem_len),
    )


def sanitize_source(source_folder: str, naming: NamingConfig) -> str:
    """清洗来源文件夹名（作为文件名的一部分）。"""
    return sanitize_stem(source_folder, replacement=naming.separator or '_', max_len=40)


def compose_base_stem(pure_name: str, source_folder: str, naming: NamingConfig) -> str:
    """拼接文件名主干（不含扩展名、不含冲突后缀）。"""
    name = sanitize_pure_name(pure_name, naming)
    source = sanitize_source(source_folder, naming) if (naming.keep_source and source_folder) else ''
    sep = naming.separator or '_'

    if not source:
        return name
    if naming.source_position == 'suffix':
        return f'{name}{sep}{source}'
    return f'{source}{sep}{name}'


def compose_filename(pure_name: str, source_folder: str, ext: str,
                     naming: NamingConfig, dest_dir: str = '') -> str:
    """生成完整文件名（已清洗、已处理长度）。"""
    stem = compose_base_stem(pure_name, source_folder, naming)
    ext = (ext or '').lower()
    if not ext.startswith('.') and ext:
        ext = '.' + ext
    filename = stem + ext
    if dest_dir:
        filename = fit_path_length(dest_dir, filename)
    return filename


def fallback_pure_name(record: ImageRecord, naming: NamingConfig) -> tuple[str, str]:
    """OCR/字段/AI 全部失败时的兜底名称。返回 (名称, 来源说明)。"""
    if naming.fallback_to_original:
        stem, _ = split_extension(record.original_name)
        return stem, '使用原文件名兜底'
    if naming.fallback_to_source and record.source_folder:
        return record.source_folder, '使用来源文件夹名兜底'
    stem, _ = split_extension(record.original_name)
    return stem, '使用原文件名兜底'


def compute_final_filename(record: ImageRecord, naming: NamingConfig,
                           dest_dir: str = '') -> str:
    """根据记录的 extracted_name 计算期望文件名（未做冲突处理）。"""
    name = record.extracted_name or ''
    if not name:
        name, _ = fallback_pure_name(record, naming)
    return compose_filename(name, record.source_folder, record.ext, naming, dest_dir)


# ---------------------------------------------------------------- 分配器

class FilenameAllocator:
    """目标目录名字分配器（防重名 / 防覆盖）。"""

    def __init__(self, policy: str = 'rename', case_insensitive: bool = True,
                 max_try: int = MAX_CONFLICT_TRY):
        self.policy = policy if policy in ('rename', 'skip') else 'rename'
        self.case_insensitive = case_insensitive
        self.max_try = max_try
        self._existing: dict[str, set[str]] = {}   # 磁盘上已有
        self._taken: dict[str, set[str]] = {}      # 本任务已分配
        self._lock = threading.RLock()

    # -------------------------------------------------- 内部

    def _key(self, name: str) -> str:
        return name.lower() if self.case_insensitive else name

    def _dir_key(self, dir_path: str) -> str:
        return file_utils.norm_path_key(dir_path)

    def _load_dir(self, dir_path: str) -> set[str]:
        """读取目录现有文件名（缓存）。"""
        dkey = self._dir_key(dir_path)
        cached = self._existing.get(dkey)
        if cached is not None:
            return cached
        names: set[str] = set()
        try:
            with os.scandir(file_utils.to_long_path(dir_path)) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False) or entry.is_dir(follow_symlinks=False):
                            names.add(self._key(entry.name))
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            pass
        except PermissionError:
            pass
        except OSError:
            pass
        self._existing[dkey] = names
        return names

    def refresh(self, dir_path: str = '') -> None:
        """丢弃缓存（目录内容在外部发生变化时调用）。"""
        with self._lock:
            if not dir_path:
                self._existing.clear()
            else:
                self._existing.pop(self._dir_key(dir_path), None)

    # -------------------------------------------------- 查询 / 预约

    def is_taken(self, dir_path: str, filename: str) -> bool:
        """文件名是否已被占用（磁盘已有 或 本任务已分配）。"""
        key = self._key(filename)
        dkey = self._dir_key(dir_path)
        with self._lock:
            if key in self._taken.get(dkey, ()):
                return True
            if key in self._load_dir(dir_path):
                return True
        # 兜底：实时检查（防缓存过期）
        return os.path.exists(file_utils.to_long_path(os.path.join(dir_path, filename)))

    def reserve(self, dir_path: str, filename: str) -> None:
        """登记一个已经/即将占用的名字。"""
        with self._lock:
            self._taken.setdefault(self._dir_key(dir_path), set()).add(self._key(filename))

    def release(self, dir_path: str, filename: str) -> None:
        """撤销登记（复制失败时回滚）。"""
        with self._lock:
            taken = self._taken.get(self._dir_key(dir_path))
            if taken:
                taken.discard(self._key(filename))

    # -------------------------------------------------- 分配

    def allocate(self, dir_path: str, desired_name: str) -> AllocationResult:
        """为 desired_name 分配一个未被占用的最终文件名。"""
        desired_name = (desired_name or '').strip()
        if not desired_name:
            return AllocationResult('', False, '文件名为空，无法分配')

        if not self.is_taken(dir_path, desired_name):
            self.reserve(dir_path, desired_name)
            return AllocationResult(desired_name, False, '')

        if self.policy == 'skip':
            return AllocationResult(desired_name, True,
                                    '目标目录已存在同名文件，按设置跳过', skipped=True)

        stem, ext = split_extension(desired_name)
        for n in range(1, self.max_try + 1):
            candidate = f'{stem}{CONFLICT_SUFFIX_SEP}{n}{ext}'
            if not self.is_taken(dir_path, candidate):
                self.reserve(dir_path, candidate)
                return AllocationResult(
                    candidate, True,
                    f'与已有文件重名，已自动改为 {candidate}')
        return AllocationResult(desired_name, True,
                                f'重名次数超过上限 {self.max_try}，无法分配', skipped=True)


# ---------------------------------------------------------------- 批量解析

def resolve_filenames(records: list[ImageRecord],
                      output_dir: str,
                      naming: NamingConfig,
                      output: OutputConfig,
                      only_selected: bool = True) -> dict:
    """为一批记录分配最终文件名（预览与执行共用同一算法，保证一致）。

    返回统计字典：{'total': n, 'renamed': n, 'skipped': n, 'empty': n}
    """
    allocator = FilenameAllocator(
        policy=output.conflict_policy,
        case_insensitive=True,          # Windows 文件系统默认不区分大小写
    )
    stats = {'total': 0, 'renamed': 0, 'skipped': 0, 'empty': 0}

    for record in records:
        if only_selected and record.status == Status.SKIPPED:
            continue
        if record.status in (Status.SUCCESS, Status.CANCELED):
            continue

        # 注意：这里不创建目录 —— 预览阶段不产生任何写操作，
        # 目标目录由 Extractor 在真正复制时才创建。
        dest_dir = record.dest_dir(output_dir, output.keep_structure)

        desired = compute_final_filename(record, naming, dest_dir)
        record.suggested_filename = desired

        if record.manual_override and record.final_filename:
            desired = fit_path_length(dest_dir, record.final_filename) \
                if dest_dir else record.final_filename

        if not desired:
            record.status = Status.NEEDS_REVIEW
            record.error = '无法生成文件名（缺少名称与兜底信息）'
            stats['empty'] += 1
            continue

        result = allocator.allocate(dest_dir, desired)
        # 冲突标记只增不减：一次重名改名的事实必须保留到报告里
        record.conflict = bool(record.conflict or result.conflict)
        if result.note:
            record.conflict_note = result.note
        record.final_filename = result.filename

        stats['total'] += 1
        if result.conflict:
            stats['renamed'] += 1
        if result.skipped:
            record.status = Status.SKIPPED
            record.error = result.note
            stats['skipped'] += 1
        elif record.status not in (Status.NEEDS_REVIEW,):
            record.status = Status.READY

    return stats


def check_conflicts(records: list[ImageRecord], output_dir: str,
                    naming: NamingConfig, output: OutputConfig) -> list[dict]:
    """只做冲突检查（不修改记录），返回冲突清单用于预览提示。"""
    allocator = FilenameAllocator(policy='rename', case_insensitive=True)
    conflicts = []
    for record in records:
        if record.status == Status.SKIPPED:
            continue
        dest_dir = record.dest_dir(output_dir, output.keep_structure)
        desired = compute_final_filename(record, naming, dest_dir)
        if not desired:
            continue
        result = allocator.allocate(dest_dir, desired)
        if result.conflict and not result.skipped:
            conflicts.append({
                'source': record.source_path,
                'original': record.original_name,
                'desired': desired,
                'final': result.filename,
                'note': result.note,
            })
    return conflicts

"""
文件扫描器（纯标准库，只读）。

核心原则：
- 扫描阶段绝对不修改任何文件（不复制、不移动、不删除、不改名）；
- 严格按模板层级匹配，而不是"递归找同名文件"；
- 单个目录/文件出错不影响整体扫描；
- 无 ** 时按模板深度"定向下钻"，性能远优于全盘递归。

性能设计：
    */1440/SAMPLE_NA.jpg
    → 只枚举根目录下的一级目录，再直接检查它们下面的 "1440" 子目录，
      不会去遍历 1440 目录下的任何子目录。
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

from models import ImageRecord, ScanResult
from core.path_matcher import PathTemplate, TemplateError, natural_key, _seg_match
from utils import file_utils

#: 每扫描多少个目录上报一次进度
PROGRESS_EVERY = 200


class ScanError(Exception):
    """扫描前置条件不满足（根目录不存在等）。"""


class Scanner:
    """按路径模板扫描图片。"""

    def __init__(self, root_dir: str, template: str,
                 max_depth: int = 32,
                 only_images: bool = True,
                 source_level: int = 0,
                 follow_symlinks: bool = False):
        self.root_dir = os.path.abspath(root_dir) if root_dir else ''
        self.template_text = template
        self.template = PathTemplate(template) if template else None
        self.max_depth = max(1, int(max_depth or 32))
        self.only_images = only_images
        self.source_level = max(0, int(source_level or 0))
        self.follow_symlinks = follow_symlinks

    # ------------------------------------------------------------ 入口

    def validate(self) -> tuple[bool, str]:
        """检查根目录与模板是否可用。"""
        if not self.root_dir:
            return False, '请先选择根目录'
        if not os.path.isdir(self.root_dir):
            return False, f'根目录不存在或不是文件夹：{self.root_dir}'
        if not self.template_text:
            return False, '请先设置路径规则'
        try:
            self.template = PathTemplate(self.template_text)
        except TemplateError as exc:
            return False, f'路径规则无效：{exc}'
        return True, ''

    def scan(self,
             progress_cb: Optional[Callable[[int, int, str], bool]] = None,
             cancel_event=None) -> ScanResult:
        """执行扫描。

        progress_cb(已扫描目录数, 已找到文件数, 当前目录) -> bool
            返回 False 表示请求取消。
        """
        ok, message = self.validate()
        if not ok:
            raise ScanError(message)

        started = time.time()
        result = ScanResult(root_dir=self.root_dir, template=self.template.raw)
        state = _ScanState(progress_cb, cancel_event)

        try:
            if self.template.has_recursive:
                records = self._scan_recursive(state)
            else:
                records = self._scan_fixed_depth(state)
        except _ScanCanceled:
            pass
        else:
            result.files = records

        result.files.sort(key=lambda r: natural_key(r.rel_path))
        for i, rec in enumerate(result.files, start=1):
            rec.index = i
        result.matched_all = len(result.files)
        result.scanned_dirs = state.dirs
        result.errors = state.errors
        result.elapsed = time.time() - started
        return result

    # ------------------------------------------------------------ 固定深度（快路径）

    def _scan_fixed_depth(self, state: '_ScanState') -> list[ImageRecord]:
        """无 ** 时：按模板层数逐层下钻，只访问可能命中的目录。"""
        segs = self.template.segments
        dir_segs = segs[:-1]
        file_seg = segs[-1]

        # 每一层的候选目录：[(目录绝对路径, 已匹配的路径段元组)]
        level: list[tuple[str, tuple]] = [(self.root_dir, ())]
        state.tick(self.root_dir, 0)

        for depth, pattern in enumerate(dir_segs):
            if depth >= self.max_depth:
                break
            next_level: list[tuple[str, tuple]] = []
            literal = self.template.literal_at(depth)
            for dir_path, parts in level:
                if state.should_stop():
                    raise _ScanCanceled()
                state.tick(dir_path, len(next_level))
                if literal is not None:
                    # 字面量层：直接拼接，不做目录枚举（关键性能优化）
                    candidate = os.path.join(dir_path, literal)
                    if self._is_dir(candidate):
                        next_level.append((candidate, parts + (literal,)))
                else:
                    for name in self._iter_subdirs(dir_path, state):
                        if _seg_match(pattern, name, self.template.case_sensitive):
                            next_level.append((os.path.join(dir_path, name), parts + (name,)))
            level = next_level
            if not level:
                break

        records: list[ImageRecord] = []
        for dir_path, parts in level:
            if state.should_stop():
                raise _ScanCanceled()
            state.tick(dir_path, len(records))
            for name in self._iter_files(dir_path, state):
                if not _seg_match(file_seg, name, self.template.case_sensitive):
                    continue
                if not self._accept_file(name):
                    continue
                records.append(self._make_record(dir_path, parts, name))
        return records

    # ------------------------------------------------------------ 递归（** 模式）

    def _scan_recursive(self, state: '_ScanState') -> list[ImageRecord]:
        """含 ** 时：遍历整棵树，再对每条相对路径做模板匹配。"""
        records: list[ImageRecord] = []
        visited: set = set()
        stack: list[tuple[str, tuple, int]] = [(self.root_dir, (), 0)]
        file_seg = self.template.segments[-1]

        while stack:
            dir_path, parts, depth = stack.pop()
            if state.should_stop():
                raise _ScanCanceled()
            state.tick(dir_path, len(records))

            key = self._dir_key(dir_path)
            if key in visited:
                continue
            visited.add(key)

            # 当前目录下的文件
            for name in self._iter_files(dir_path, state):
                if not _seg_match(file_seg, name, self.template.case_sensitive):
                    continue
                if not self._accept_file(name):
                    continue
                full_parts = parts + (name,)
                if self.template.match_parts(list(full_parts)):
                    records.append(self._make_record(dir_path, parts, name))

            # 子目录
            if depth >= self.max_depth:
                state.record_error(dir_path, f'超过最大递归深度 {self.max_depth}，已跳过')
                continue
            for name in self._iter_subdirs(dir_path, state):
                if name.startswith('$'):
                    continue
                stack.append((os.path.join(dir_path, name), parts + (name,), depth + 1))

        return records

    # ------------------------------------------------------------ 目录/文件枚举

    def _is_dir(self, path: str) -> bool:
        try:
            return os.path.isdir(file_utils.to_long_path(path))
        except OSError:
            return False

    def _dir_key(self, path: str):
        """防环键：优先用 (设备号, inode)，不可用时退化为绝对路径。"""
        try:
            st = os.stat(file_utils.to_long_path(path), follow_symlinks=self.follow_symlinks)
            if st.st_ino:
                return (st.st_dev, st.st_ino)
        except OSError:
            pass
        return file_utils.norm_path_key(path)

    def _iter_subdirs(self, dir_path: str, state: '_ScanState') -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(file_utils.to_long_path(dir_path)) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=self.follow_symlinks):
                            continue
                        # 跳过重解析点（junction/symlink）避免循环
                        if getattr(entry, 'is_symlink', lambda: False)() and not self.follow_symlinks:
                            continue
                    except OSError:
                        continue
                    names.append(entry.name)
        except PermissionError:
            state.record_error(dir_path, '没有访问权限')
            return []
        except FileNotFoundError:
            return []
        except OSError as exc:
            state.record_error(dir_path, f'读取目录失败：{exc}')
            return []
        names.sort(key=natural_key)
        return names

    def _iter_files(self, dir_path: str, state: '_ScanState') -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(file_utils.to_long_path(dir_path)) as it:
                for entry in it:
                    try:
                        if not entry.is_file(follow_symlinks=self.follow_symlinks):
                            continue
                    except OSError:
                        continue
                    names.append(entry.name)
        except PermissionError:
            state.record_error(dir_path, '没有访问权限')
            return []
        except OSError:
            return []
        names.sort(key=natural_key)
        return names

    def _accept_file(self, name: str) -> bool:
        """是否接受该文件（可选：仅图片）。"""
        if not self.only_images:
            return True
        return file_utils.is_image_file(name)

    # ------------------------------------------------------------ 记录构造

    def _make_record(self, dir_path: str, parts: tuple, file_name: str) -> ImageRecord:
        full_path = os.path.join(dir_path, file_name)
        rel_parts = list(parts) + [file_name]
        rel_path = '/'.join(rel_parts)
        source_folder = parts[self.source_level] if len(parts) > self.source_level else (
            parts[0] if parts else '')

        size, mtime = 0, 0.0
        try:
            st = os.stat(file_utils.to_long_path(full_path))
            size, mtime = int(st.st_size), float(st.st_mtime)
        except OSError:
            pass

        return ImageRecord(
            source_path=full_path,
            rel_path=rel_path,
            source_folder=source_folder,
            original_name=file_name,
            ext=os.path.splitext(file_name)[1].lower(),
            size=size,
            mtime=mtime,
        )


# ---------------------------------------------------------------- 内部状态

class _ScanCanceled(Exception):
    pass


class _ScanState:
    """扫描过程中的进度与错误收集。"""

    def __init__(self, progress_cb, cancel_event):
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.dirs = 0
        self.errors: list[tuple[str, str]] = []
        self._last_report = 0

    def tick(self, current_dir: str, found: int) -> None:
        self.dirs += 1
        if self.progress_cb and self.dirs - self._last_report >= PROGRESS_EVERY:
            self._last_report = self.dirs
            try:
                keep_going = self.progress_cb(self.dirs, found, current_dir)
                if keep_going is False:
                    raise _ScanCanceled()
            except _ScanCanceled:
                raise
            except Exception:
                pass

    def should_stop(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        if self.progress_cb is not None and self.dirs % PROGRESS_EVERY == 0:
            self._last_report = self.dirs
            try:
                if self.progress_cb(self.dirs, 0, '') is False:
                    return True
            except Exception:
                pass
        return False

    def record_error(self, path: str, message: str) -> None:
        if len(self.errors) < 500:
            self.errors.append((path, message))


# ---------------------------------------------------------------- 便捷函数

def quick_scan(root_dir: str, template: str, only_images: bool = True) -> ScanResult:
    """一次性扫描（同步，供测试与命令行使用）。"""
    return Scanner(root_dir, template, only_images=only_images).scan()


def preview_rule(root_dir: str, template: str, limit: int = 20) -> tuple[list[str], str]:
    """预览规则效果：返回 (前 N 条相对路径, 说明文本)。"""
    try:
        result = Scanner(root_dir, template).scan()
    except ScanError as exc:
        return [], str(exc)
    return [r.rel_path for r in result.files[:limit]], ''


def count_only(root_dir: str, template: str) -> int:
    """只统计数量。"""
    try:
        return Scanner(root_dir, template).scan().count
    except ScanError:
        return 0

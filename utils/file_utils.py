"""
文件系统工具（纯标准库；图像读取部分按需 import Pillow）。

安全原则：
- 复制使用 os.O_EXCL 独占创建 —— 目标已存在时由操作系统报错，物理上不可能覆盖；
- 所有写入类操作都接受 `dry_run` 或由上层控制，本模块不主动删除任何文件；
- 支持 Windows 长路径（\\\\?\\ 前缀）与中文路径。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

COPY_CHUNK = 1024 * 512

#: 图片扩展名集合（用于快速过滤）
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff',
    '.webp', '.jfif', '.heic', '.heif', '.avif',
}


# ---------------------------------------------------------------- 路径

def is_windows() -> bool:
    return os.name == 'nt'


def to_long_path(path: str | os.PathLike) -> str:
    r"""转为 Windows 扩展长度路径（\\?\ 前缀），突破 260 字符限制。

    非 Windows 平台原样返回。已经是扩展路径或相对路径不含盘符时按情况处理。
    """
    p = os.fspath(path)
    if not is_windows():
        return p
    if p.startswith('\\\\?\\'):
        return p
    p = os.path.abspath(p)
    if p.startswith('\\\\'):                      # UNC: \\server\share
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p.replace('/', '\\')


def ensure_dir(path: str | os.PathLike) -> str:
    """确保目录存在，返回绝对路径字符串。"""
    p = os.path.abspath(os.fspath(path))
    os.makedirs(to_long_path(p), exist_ok=True)
    return p


def exists(path: str | os.PathLike) -> bool:
    try:
        return os.path.exists(to_long_path(path))
    except (OSError, ValueError):
        return False


def get_app_data_dir() -> str:
    """应用数据目录（日志 / 数据库 / 配置）。"""
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if not base:
        base = os.path.expanduser('~')
    return ensure_dir(os.path.join(base, 'PicHarvest'))


def get_log_dir() -> str:
    return ensure_dir(os.path.join(get_app_data_dir(), 'logs'))


def get_db_path() -> str:
    return os.path.join(get_app_data_dir(), 'history.db')


def get_config_path() -> str:
    return os.path.join(get_app_data_dir(), 'config.json')


def human_size(num_bytes: int | float | None) -> str:
    """字节数转可读字符串。"""
    if num_bytes is None:
        return '-'
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def free_space(path: str) -> int:
    """返回目标路径所在磁盘剩余空间（字节）；失败返回 -1。"""
    try:
        probe = path
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return shutil.disk_usage(to_long_path(probe or '.')).free
    except OSError:
        return -1


def list_subdir_names(path: str) -> list[str]:
    """列出直接子目录名（只读，稳定排序，忽略无权限项）。"""
    names: list[str] = []
    try:
        with os.scandir(to_long_path(path)) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(names)


def list_file_names(path: str, extensions: set[str] | None = None) -> list[str]:
    """列出直接子文件名（只读，稳定排序）。"""
    names: list[str] = []
    try:
        with os.scandir(to_long_path(path)) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if extensions:
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in extensions:
                        continue
                names.append(entry.name)
    except OSError:
        return []
    return sorted(names)


def unique_sorted_dir_children(path: str) -> list[str]:
    """返回子目录名去重列表（用于普通模式下拉框）。"""
    return sorted(set(list_subdir_names(path)))


# ---------------------------------------------------------------- 复制（绝不覆盖）

class FileExistsError_(FileExistsError):
    """目标文件已存在（独占创建失败）。"""


def copy_no_overwrite(src: str, dst: str) -> None:
    """把 src 复制到 dst，目标已存在时抛出 FileExistsError。

    使用 O_CREAT|O_EXCL 让操作系统内核保证"不存在才创建"，
    从根本上杜绝并发/竞态导致的覆盖。内容写完后尝试复制元数据。
    """
    src_long = to_long_path(src)
    dst_long = to_long_path(dst)

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
    try:
        fd = os.open(dst_long, flags, 0o666)
    except FileExistsError as exc:
        raise FileExistsError_(f'目标文件已存在：{dst}') from exc

    success = False
    try:
        with os.fdopen(fd, 'wb') as out_f, open(src_long, 'rb') as in_f:
            shutil.copyfileobj(in_f, out_f, COPY_CHUNK)
            out_f.flush()
            try:
                os.fsync(out_f.fileno())
            except OSError:
                pass
        success = True
    finally:
        if not success:
            # 复制失败 -> 清理半成品，避免留下残缺文件
            try:
                os.remove(dst_long)
            except OSError:
                pass

    # 保留时间戳（失败不影响主流程）
    try:
        shutil.copystat(src_long, dst_long)
    except OSError:
        pass


def safe_delete(path: str) -> tuple[bool, str]:
    """删除单个文件（仅用于撤销本任务生成的文件）。返回 (成功, 错误信息)。"""
    try:
        from send2trash import send2trash  # 可选：优先送回收站
        send2trash(os.path.abspath(os.fspath(path)))
        return True, ''
    except ImportError:
        pass
    except Exception as exc:  # send2trash 自身异常则退化为普通删除
        pass

    try:
        os.remove(to_long_path(path))
        return True, ''
    except FileNotFoundError:
        return True, ''
    except OSError as exc:
        return False, str(exc)


# ---------------------------------------------------------------- 图像读取

class ImageLoadError(Exception):
    """图片无法读取（损坏 / 非图片 / 无权限）。"""


def load_image_rgb(path: str):
    """读取图片为 PIL.Image（RGB，已按 EXIF 方向纠正）。

    使用 Pillow 而不是 cv2.imread，因为 cv2.imread 无法处理中文路径。
    返回的 Image 由调用方负责 close。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise ImageLoadError('未安装 Pillow，无法读取图片') from exc

    try:
        img = Image.open(path)          # Pillow 内部使用宽字符 API，支持中文路径
    except FileNotFoundError as exc:
        raise ImageLoadError('文件不存在') from exc
    except PermissionError as exc:
        raise ImageLoadError('没有读取权限') from exc
    except OSError as exc:
        raise ImageLoadError(f'无法识别的图片格式或文件已损坏：{exc}') from exc

    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    try:
        return img.convert('RGB')
    except Exception as exc:
        raise ImageLoadError(f'图片转换失败：{exc}') from exc


def get_image_size(path: str) -> tuple[int, int] | None:
    """快速获取图片宽高（只读文件头），失败返回 None。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return None
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


# ---------------------------------------------------------------- 其它

def file_fingerprint(path: str) -> tuple[int, float] | None:
    """返回 (大小, 修改时间)，用于撤销前的安全校验。"""
    try:
        st = os.stat(to_long_path(path))
        return int(st.st_size), float(st.st_mtime)
    except OSError:
        return None


def open_in_explorer(path: str) -> bool:
    """在资源管理器中打开目录 / 定位文件。"""
    target = os.path.abspath(os.fspath(path))
    try:
        if is_windows():
            if os.path.isfile(target):
                os.system(f'explorer /select,"{target}"')
            else:
                os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            os.system(f'open "{target}"')
        else:
            os.system(f'xdg-open "{target}"')
        return True
    except Exception:
        return False


def write_text(path: str, text: str, encoding: str = 'utf-8-sig') -> None:
    """写文本文件（utf-8-sig 便于 Excel 正确打开 CSV）。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(to_long_path(path), 'w', encoding=encoding, newline='') as f:
        f.write(text)


def make_relative(path: str, root: str) -> str:
    """返回相对路径（统一使用 / 分隔符，便于模板匹配）。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:                    # 跨盘符
        return os.path.basename(path)
    return rel.replace('\\', '/')


def norm_path_key(path: str) -> str:
    """用于比较的路径键（绝对、小写、统一分隔符）。"""
    return os.path.normcase(os.path.abspath(os.fspath(path))).replace('\\', '/')

"""
文件名与输入校验工具（纯标准库，无第三方依赖）。

设计要点（Windows 文件系统）：
1. 非法字符 \\ / : * ? " < > | 以及控制字符必须替换；
2. 结尾的点号与空格在 Windows 上会被静默丢弃，必须主动去掉；
3. CON / NUL / COM1 ... 等保留设备名不可作为文件名；
4. 文件名长度需留出目录部分的空间，默认限制 stem 长度；
5. 大小写不敏感比较（与 NTFS 默认行为一致）。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- 常量

#: Windows 非法文件名字符
ILLEGAL_CHARS = '\\/:*?"<>|'

_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_WS_RE = re.compile(r'\s+')
_MULTI_UNDERSCORE_RE = re.compile(r'_{2,}')
_LEADING_TRAILING_DOTS_SPACES_RE = re.compile(r'^[\s.]+|[\s.]+$')

#: Windows 保留设备名（不区分大小写，带扩展名同样非法）
RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
}

#: 单个文件名（不含扩展名）的推荐最大长度
DEFAULT_MAX_STEM_LEN = 100
#: 完整路径的保守上限（未开启 LongPathsEnabled 时 Windows 为 260）
SAFE_PATH_LIMIT = 240

#: 名称全部被清洗掉时使用的兜底名字
FALLBACK_STEM = '未命名'


# ---------------------------------------------------------------- 基础清洗

def fullwidth_to_halfwidth(text: str) -> str:
    """全角字符转半角（OCR 结果中常见全角冒号、全角字母数字）。

    注意：只转换 ASCII 可见区间的全角形式（U+FF01-U+FF5E）和全角空格，
    中文汉字不受影响。
    """
    if not text:
        return ''
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:            # 全角空格
            out.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ! ~
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return ''.join(out)


def strip_control_chars(text: str) -> str:
    """去掉控制字符（换行、制表符、不可见字符等）。"""
    if not text:
        return ''
    return _CONTROL_RE.sub(' ', text)


def collapse_whitespace(text: str) -> str:
    """把连续空白压缩成单个半角空格，并去掉首尾空白。"""
    if not text:
        return ''
    text = text.replace('　', ' ')
    return _WS_RE.sub(' ', text).strip()


def clean_text(text: str) -> str:
    """通用文本清洗：全角转半角 → 去控制字符 → 压缩空白。"""
    return collapse_whitespace(strip_control_chars(fullwidth_to_halfwidth(text or '')))


def truncate(text: str, max_len: int) -> str:
    """按字符截断（Python 字符串按码点，不会截坏 UTF-8 编码）。"""
    if max_len <= 0 or text is None:
        return ''
    return text if len(text) <= max_len else text[:max_len]


# ---------------------------------------------------------------- 文件名清洗

def sanitize_stem(
    stem: str,
    replacement: str = '_',
    max_len: int = DEFAULT_MAX_STEM_LEN,
    collapse_repeats: bool = True,
) -> str:
    """把任意文本清洗为合法的 Windows 文件名主干（不含扩展名）。

    - 非法字符替换为 replacement
    - 去掉首尾的点与空格
    - 折叠重复的分隔符
    - 处理保留设备名
    - 超长截断
    """
    if stem is None:
        return FALLBACK_STEM

    # 1. 全角转半角 + 去掉换行/制表符
    text = fullwidth_to_halfwidth(str(stem))
    text = _CONTROL_RE.sub(' ', text)

    # 2. 非法字符替换
    if replacement is None:
        replacement = ''
    text = _ILLEGAL_RE.sub(replacement, text)

    # 3. 压缩空白与重复分隔符
    text = _WS_RE.sub(' ', text)
    if replacement:
        # 连续多个分隔符折叠为一个（'a__b' -> 'a_b'）
        text = re.sub(re.escape(replacement) + r'{2,}', replacement, text)
        # 分隔符与空格相邻时也折叠
        text = re.sub(r'\s*' + re.escape(replacement) + r'\s*', replacement, text)
        if collapse_repeats:
            text = _MULTI_UNDERSCORE_RE.sub('_', text) if replacement == '_' else text

    # 4. 去掉首尾的点/空格/分隔符
    text = _LEADING_TRAILING_DOTS_SPACES_RE.sub('', text)
    if replacement:
        text = text.strip(replacement)

    # 5. 折叠后可能再次出现首尾空格
    text = text.strip()

    # 6. 空则兜底
    if not text or set(text) <= {'.', ' '}:
        return FALLBACK_STEM

    # 7. 保留设备名
    if is_reserved_name(text):
        text = text + '_'

    # 8. 截断
    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip(' .')

    return text or FALLBACK_STEM


def sanitize_filename(filename: str, replacement: str = '_',
                      max_stem_len: int = DEFAULT_MAX_STEM_LEN) -> str:
    """清洗完整文件名（保留扩展名）。"""
    if not filename:
        return FALLBACK_STEM
    stem, ext = split_extension(filename)
    stem = sanitize_stem(stem, replacement=replacement, max_len=max_stem_len)
    return stem + ext


def split_extension(filename: str) -> tuple[str, str]:
    """拆分主名与扩展名。`a.b.jpg` -> ('a.b', '.jpg')。"""
    if not filename:
        return '', ''
    idx = filename.rfind('.')
    if idx <= 0:                      # 无扩展名，或形如 '.gitignore'
        return filename, ''
    return filename[:idx], filename[idx:]


def normalize_extension(ext: str) -> str:
    """扩展名规范化为小写且带点。"""
    ext = (ext or '').strip()
    if not ext:
        return ''
    if not ext.startswith('.'):
        ext = '.' + ext
    return ext.lower()


def is_reserved_name(stem: str) -> bool:
    """是否为 Windows 保留设备名（只看第一个点之前的部分）。"""
    if not stem:
        return False
    head = stem.split('.')[0].strip().upper()
    return head in RESERVED_NAMES


def is_safe_filename(filename: str) -> tuple[bool, str]:
    """校验文件名是否可直接用于 Windows。返回 (是否安全, 原因)。"""
    if not filename or not filename.strip():
        return False, '文件名为空'
    if _ILLEGAL_RE.search(filename):
        return False, '包含 Windows 非法字符 \\ / : * ? " < > |'
    if filename.endswith('.') or filename.endswith(' '):
        return False, '文件名不能以点或空格结尾'
    stem, _ = split_extension(filename)
    if not stem:
        return False, '缺少文件主名'
    if is_reserved_name(stem):
        return False, f'"{stem}" 是 Windows 保留设备名'
    if len(filename) > 255:
        return False, '文件名过长（超过 255 字符）'
    return True, ''


def sanitize_directory_name(name: str, replacement: str = '_') -> str:
    """清洗目录名（与文件名规则基本一致）。"""
    return sanitize_stem(name, replacement=replacement, max_len=80)


# ---------------------------------------------------------------- 路径长度

def fit_path_length(directory: str, filename: str,
                    limit: int = SAFE_PATH_LIMIT) -> str:
    """在不超长前提下压缩文件名主干，返回可用文件名。

    若目录本身已超长，则返回清洗后的原名（此时需依赖长路径支持）。
    """
    stem, ext = split_extension(filename)
    room = limit - len(directory) - len(ext) - 2
    if room <= 5:
        return filename
    if len(stem) <= room:
        return filename
    stem = stem[:room].rstrip(' ._')
    if not stem:
        stem = FALLBACK_STEM
    return stem + ext


# ---------------------------------------------------------------- 字段解析辅助

def normalize_for_compare(text: str) -> str:
    """用于把标签与文本做宽松比较：全角转半角、去空白、去冒号、转小写。"""
    text = fullwidth_to_halfwidth(text or '')
    text = _WS_RE.sub('', text)
    text = text.replace('：', ':').replace(':', '')
    return text.strip().lower()


def is_mostly_cjk(text: str) -> bool:
    """判断字符串是否以中日韩字符为主。"""
    text = (text or '').strip()
    if not text:
        return False
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    return cjk / len(text) >= 0.5


def count_digits(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def has_unusual_symbols(text: str) -> bool:
    """是否包含大量非常见符号（常用于排除整段 OCR 噪声）。"""
    if not text:
        return False
    unusual = sum(1 for ch in text
                  if not ch.isalnum() and ch not in ' ._-()（）【】[]·')
    return unusual / max(len(text), 1) > 0.4


def is_unicode_category(chat: str, prefix: str) -> bool:
    try:
        return unicodedata.category(chat).startswith(prefix)
    except Exception:  # pragma: no cover - 极端异常保护
        return False

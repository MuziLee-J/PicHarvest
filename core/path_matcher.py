"""
路径模板解析与匹配（纯标准库）。

模板语义（与需求书第四节一致）：
    *   匹配"恰好一层"目录名（不跨分隔符），可出现在任意层
    **  匹配"零层或多层"目录（递归）
    ?   匹配单个字符
    最后一段始终是文件名模式，例如 SAMPLE_NA.jpg 或 *.jpg

示例：
    */1440/SAMPLE_NA.jpg
        ✅ CASE001/1440/SAMPLE_NA.jpg
        ❌ CASE001/2880/SAMPLE_NA.jpg
        ❌ x/CASE001/1440/SAMPLE_NA.jpg   （多了一层）
    **/1440/SAMPLE_NA.jpg
        ✅ 上面两条都命中

Windows 默认大小写不敏感比较（与 NTFS 行为一致）。
"""

from __future__ import annotations

import fnmatch
import os
import re

#: 通配符只支持 * 与 ?；[ ] 一律按普通字符处理
#: （真实文件夹名里经常出现 []，例如 AB[1]，按通配符解析会误匹配）
_WILDCARD_CHARS = set('*?')
_SEGMENT_SPLIT_RE = re.compile(r'[\\/]+')


class TemplateError(ValueError):
    """模板非法。"""


# ---------------------------------------------------------------- 工具

def _has_wildcard(segment: str) -> bool:
    return any(ch in _WILDCARD_CHARS for ch in segment)


def _glob_match(pattern: str, name: str) -> bool:
    """单段通配符匹配：* 匹配任意字符序列，? 匹配单个字符。

    自己实现而不用 fnmatch，是为了让 [ ] 保持字面含义。
    """
    pi = ni = 0
    star = -1
    mark = 0
    p_len, n_len = len(pattern), len(name)
    while ni < n_len:
        if pi < p_len and (pattern[pi] == '?' or pattern[pi] == name[ni]):
            pi += 1
            ni += 1
        elif pi < p_len and pattern[pi] == '*':
            star = pi
            mark = ni
            pi += 1
        elif star >= 0:
            pi = star + 1
            mark += 1
            ni = mark
        else:
            return False
    while pi < p_len and pattern[pi] == '*':
        pi += 1
    return pi == p_len


def normalize_template(template: str) -> str:
    """模板规范化：统一分隔符、去首尾斜杠、压缩重复斜杠。"""
    text = (template or '').strip().strip('"').strip("'")
    text = text.replace('\\', '/')
    text = _SEGMENT_SPLIT_RE.sub('/', text)
    text = text.strip('/')
    return text


def template_segments(template: str) -> list[str]:
    """拆分为段落列表（已规范化）。"""
    text = normalize_template(template)
    if not text:
        return []
    return [seg.strip() for seg in text.split('/') if seg.strip()]


def relativize_template(template: str, root_dir: str) -> str:
    """若用户直接粘贴了绝对路径，且位于根目录之下，则转为相对模板。"""
    if not template or not root_dir:
        return normalize_template(template)
    text = normalize_template(template)
    root_norm = normalize_template(os.path.abspath(root_dir).replace('\\', '/'))
    low_text, low_root = text.lower(), root_norm.lower()
    if low_text.startswith(low_root + '/'):
        return text[len(root_norm) + 1:]
    # 用户只写了根目录的最后一段
    root_name = os.path.basename(root_norm)
    if root_name and low_text.startswith(root_name.lower() + '/'):
        return text[len(root_name) + 1:]
    return text


def _seg_match(pattern: str, name: str, case_sensitive: bool = False) -> bool:
    """单段匹配。无通配符时退化为精确比较。"""
    if pattern == '**':
        return True
    if not case_sensitive:
        pattern = pattern.lower()
        name = name.lower()
    if not _has_wildcard(pattern):
        return pattern == name
    return _glob_match(pattern, name)


def natural_key(text: str):
    """自然排序键：CASE2 排在 CASE10 之前。"""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', text or '')]


# ---------------------------------------------------------------- 模板对象

class PathTemplate:
    """路径模板对象。"""

    def __init__(self, template: str, case_sensitive: bool = False):
        self.raw = normalize_template(template)
        self.segments = template_segments(self.raw)
        self.case_sensitive = case_sensitive
        if not self.segments:
            raise TemplateError('路径模板为空')
        for seg in self.segments[:-1]:
            if '.' in seg and _has_wildcard(seg) and seg.startswith('*.'):
                # 形如 "*/1440/*.jpg/xxx" 的写法几乎肯定是配置失误
                raise TemplateError(f'目录层 "{seg}" 看起来像文件名模式，请检查模板层级')

    # -------------------------------------------------- 基本属性

    @property
    def directory_segments(self) -> list[str]:
        """目录层（不含文件名层）。"""
        return self.segments[:-1]

    @property
    def filename_pattern(self) -> str:
        return self.segments[-1]

    @property
    def has_recursive(self) -> bool:
        """是否含 **（无限层级）。"""
        return '**' in self.segments

    @property
    def depth(self) -> int:
        """模板固定层数（含文件名层）。"""
        return len(self.segments)

    def max_depth(self) -> int | None:
        """扫描深度上限；含 ** 时返回 None（由调用方限制）。"""
        return None if self.has_recursive else len(self.segments)

    def star_indices(self) -> list[int]:
        """所有 * / ** 所在层号（用于来源文件夹候选）。"""
        return [i for i, seg in enumerate(self.segments)
                if _has_wildcard(seg) or seg == '**']

    def literal_at(self, index: int) -> str | None:
        """第 index 层若是纯字面量则返回，否则 None。"""
        if 0 <= index < len(self.segments) and not _has_wildcard(self.segments[index]):
            return self.segments[index]
        return None

    def filename_has_extension(self) -> bool:
        """文件名层是否指定了明确扩展名。"""
        return bool(os.path.splitext(self.filename_pattern)[1])

    def literal_extension(self) -> str:
        ext = os.path.splitext(self.filename_pattern)[1]
        return ext if _has_wildcard(ext) is False else ''

    # -------------------------------------------------- 匹配

    def match_parts(self, parts: list[str]) -> bool:
        """按层级匹配（parts 为相对路径的各段，最后一段是文件名）。"""
        pat = self.segments
        n, m = len(pat), len(parts)
        if n > 64 or m > 128:                     # 防御性上限
            return False
        # 动态规划：dp[i][j] 表示模板前 i 段能否匹配路径前 j 段
        dp = [[False] * (m + 1) for _ in range(n + 1)]
        dp[0][0] = True
        for i in range(1, n + 1):
            seg = pat[i - 1]
            if seg == '**':
                dp[i][0] = dp[i - 1][0]
            for j in range(1, m + 1):
                if seg == '**':
                    dp[i][j] = dp[i - 1][j] or dp[i][j - 1]
                else:
                    dp[i][j] = dp[i - 1][j - 1] and _seg_match(
                        seg, parts[j - 1], self.case_sensitive)
        return dp[n][m]

    def match(self, rel_path: str) -> bool:
        """按相对路径（任意分隔符）匹配。"""
        parts = [p for p in _SEGMENT_SPLIT_RE.split(normalize_template(rel_path)) if p]
        return self.match_parts(parts)

    # -------------------------------------------------- 展示

    def describe(self) -> str:
        """人类可读说明。"""
        lines = [f'模板：{self.raw}', f'层数：{self.depth} 层（含文件名）']
        if self.has_recursive:
            lines.append('模式：递归匹配（** 表示任意层级）')
        else:
            lines.append('模式：严格层级匹配（* 只匹配一层目录）')
        lines.append(f'文件名模式：{self.filename_pattern}')
        return '\n'.join(lines)

    def example(self) -> str:
        """生成一个示例相对路径，用于界面提示。"""
        samples = []
        for i, seg in enumerate(self.segments):
            if seg == '**':
                samples.append('任意层级')
            elif _has_wildcard(seg):
                samples.append('CASE001' if i == 0 else 'xxx')
            else:
                samples.append(seg)
        return '/'.join(samples)

    def __repr__(self) -> str:  # pragma: no cover
        return f'<PathTemplate {self.raw!r} depth={self.depth}>'


# ---------------------------------------------------------------- 构造与校验

def build_template(level1: str, level2: str, file_name: str, mode: str = 'basic',
                   template: str = '') -> str:
    """根据普通模式的三段式下拉框构造路径模板。

    level1/level2 为空或 '*' 时表示"所有文件夹"。
    """
    if mode == 'advanced':
        return normalize_template(template)
    parts = []
    parts.append((level1 or '*').strip() or '*')
    if (level2 or '').strip():
        parts.append(level2.strip())
    parts.append((file_name or '*').strip() or '*')
    return '/'.join(parts)


def parse_template_to_basic(template: str) -> dict:
    """把模板反向解析为普通模式的三段式（供界面切换模式时使用）。"""
    segs = template_segments(template)
    result = {'level1': '*', 'level2': '', 'file_name': ''}
    if not segs:
        return result
    result['file_name'] = segs[-1]
    dirs = segs[:-1]
    if dirs:
        result['level1'] = dirs[0]
    if len(dirs) >= 2:
        result['level2'] = dirs[1]
    return result


def validate_template(template: str) -> tuple[bool, str]:
    """校验模板合法性，返回 (是否可用, 提示信息)。"""
    text = normalize_template(template)
    if not text:
        return False, '路径模板不能为空'
    if re.match(r'^[A-Za-z]:', text):
        return False, '模板应为相对路径（不要以盘符开头），例如 */1440/a.jpg'
    if text.startswith('//'):
        return False, '模板不应以网络路径开头'
    segs = template_segments(text)
    if not segs:
        return False, '路径模板不能为空'
    for seg in segs:
        if seg.strip() in ('', '.', '..'):
            return False, f'存在非法层：{seg!r}'
        if any(ch in seg for ch in '<>"|'):
            return False, f'层 "{seg}" 含有非法字符（< > " |）'
    if segs[-1] == '**':
        return False, '最后一段必须是文件名模式，不能是 **'
    try:
        tpl = PathTemplate(text)
    except TemplateError as exc:
        return False, str(exc)
    if not _has_wildcard(segs[0]) and len(segs) == 1:
        return True, '提示：模板只有一层，将在根目录下直接匹配文件'
    if tpl.has_recursive:
        return True, '含 ** 递归匹配，扫描范围较大，请确认根目录规模'
    return True, f'严格匹配 {tpl.depth} 层路径'


def describe_template_effect(template: str, root_name: str = '根目录') -> str:
    """生成"准备从哪里提取什么"的说明文本。"""
    try:
        tpl = PathTemplate(template)
    except TemplateError as exc:
        return f'模板无效：{exc}'
    parts = []
    for i, seg in enumerate(tpl.segments[:-1]):
        if seg == '**':
            parts.append('任意层级目录')
        elif _has_wildcard(seg):
            parts.append('任意文件夹' if i == 0 else '任意子文件夹')
            parts[-1] = f'第{i + 1}层：任意文件夹'
        else:
            parts.append(f'第{i + 1}层：必须是 "{seg}"')
    if not parts:
        parts.append('直接在根目录下')
    return (f'在「{root_name}」下查找：' + ' → '.join(parts) +
            f' → 文件名为 "{tpl.filename_pattern}"')

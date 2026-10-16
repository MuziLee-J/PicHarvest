#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提交前自检：确认仓库里没有混入内部编号、代号、批次日期等敏感标识。

设计要点 —— **按"编号形态"匹配，不硬编码任何具体值**。
如果把要保密的原始串直接写进检查规则，那规则文件本身就成了泄露源
（本脚本的前一版就犯过这个错，所以特意改成形态匹配）。

检查两类目标：
    1. 源码树：所有文本文件
    2. 打包产物（可选）：解出 PyInstaller 归档里的 Python 模块常量

用法：
    python tools/check_anonymity.py                    # 只查源码树
    python tools/check_anonymity.py --exe dist/PicHarvest/PicHarvest.exe
    python tools/check_anonymity.py --allow-placeholders   # 允许占位符（默认即允许）
"""

from __future__ import annotations

import argparse
import os
import re
import sys

SKIP_DIRS = {'.git', '.venv', 'build', 'dist', '__pycache__', '_testdata'}

TEXT_EXTENSIONS = {'.py', '.md', '.txt', '.spec', '.json', '.bat', '.cmd',
                   '.gitignore', '.gitattributes', '.yml', '.yaml', ''}

# ---------------------------------------------------------------------------
# 敏感形态规则：(正则, 说明)
#
# 这些形态覆盖"内部编号"的典型写法，不会匹配到本项目的正常内容：
#   PySide6 / sqlite3 / CLI / v1.0.0 / GUI 等都不符合
# ---------------------------------------------------------------------------
SUSPICIOUS_PATTERNS = [
    (r'\b[A-Za-z]{2,4}\d{6,}\b', '疑似内部编号（2-4 个字母 + 6 位以上数字）'),
    # 负向后顾：避免把 IMAGE-DATA-1234 这类复合串里的片段误判为代号
    (r'(?<![\w-])[A-Za-z]{2,4}-\d{2,}[A-Za-z0-9]*\b',
     '疑似产品/项目代号（字母-数字）'),
    (r'\b(?:19|20)\d{6}\b', '疑似批次日期目录（8 位日期）'),
]

#: 允许出现的形态（中性占位符与常见技术术语）
ALLOWED = {
    '20250101',        # 示例批次日期（占位符）
}

#: exe 检查只扫我们自己的模块。
#: 标准库与第三方库里大量字符串都"看着像代号"（编码名、RFC 编号等），
#: 全扫会被噪声淹没，反而看不出真正的问题。
OWN_PACKAGES = {'core', 'ocr', 'ui', 'utils', 'database', 'reports',
                'models', 'main', 'tools'}


def is_allowed(token: str) -> bool:
    return token.upper() in {item.upper() for item in ALLOWED}


def iter_text_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1] in TEXT_EXTENSIONS:
                yield os.path.join(dirpath, name)


def scan_source_tree(root: str) -> list:
    """扫描源码树，返回 [(相对路径, 行号, 命中内容, 说明)]。"""
    findings = []
    for path in iter_text_files(root):
        try:
            with open(path, encoding='utf-8') as handle:
                lines = handle.readlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, start=1):
            for pattern, label in SUSPICIOUS_PATTERNS:
                for match in re.finditer(pattern, line):
                    token = match.group(0)
                    if is_allowed(token):
                        continue
                    findings.append((os.path.relpath(path, root), number,
                                     token, label))
    return findings


def scan_exe(exe_path: str) -> list:
    """扫描打包产物：解出 PyInstaller 归档中的模块常量。

    直接 grep 二进制是搜不到的（模块被 zlib 压缩），
    必须用归档读取器反序列化，否则会得到"干净"的假结论。
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError:
        print('⚠ 未安装 PyInstaller，跳过 exe 检查')
        return []

    import marshal
    import tempfile

    def walk_consts(code):
        for const in getattr(code, 'co_consts', ()):
            if isinstance(const, str):
                yield const
            elif hasattr(const, 'co_consts'):
                yield from walk_consts(const)

    reader = CArchiveReader(exe_path)
    pyz_names = [name for name in reader.toc if str(name).endswith('.pyz')]
    if not pyz_names:
        return []

    handle = tempfile.NamedTemporaryFile(suffix='.pyz', delete=False)
    try:
        handle.write(reader.extract(pyz_names[0]))
        handle.close()
        archive = ZlibArchiveReader(handle.name)
        findings = []
        scanned = 0
        for name in archive.toc:
            if str(name).split('.')[0] not in OWN_PACKAGES:
                continue                      # 跳过标准库与第三方模块
            scanned += 1
            try:
                code = archive.extract(name)
            except Exception:
                continue
            if isinstance(code, (bytes, bytearray)):
                try:
                    code = marshal.loads(code)
                except Exception:
                    continue
            for text in walk_consts(code):
                for pattern, label in SUSPICIOUS_PATTERNS:
                    for match in re.finditer(pattern, text):
                        token = match.group(0)
                        if not is_allowed(token):
                            findings.append((name, 0, token, label))
        return findings
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description='内部标识残留检查')
    parser.add_argument('--exe', help='同时检查打包后的 exe')
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    failed = False

    print('=' * 62)
    print('源码树检查')
    print('=' * 62)
    findings = scan_source_tree(root)
    if findings:
        failed = True
        print(f'✘ 发现 {len(findings)} 处疑似内部标识：')
        for path, number, token, label in findings[:40]:
            print(f'    {path}:{number}  "{token}"  （{label}）')
        if len(findings) > 40:
            print(f'    … 还有 {len(findings) - 40} 处')
    else:
        print('✔ 未发现疑似内部标识')

    if args.exe:
        print()
        print('=' * 62)
        print(f'打包产物检查：{args.exe}')
        print('=' * 62)
        if not os.path.isfile(args.exe):
            print(f'✘ 找不到文件：{args.exe}')
            return 2
        exe_findings = scan_exe(args.exe)
        if exe_findings:
            failed = True
            print(f'✘ 发现 {len(exe_findings)} 处疑似内部标识：')
            for name, _number, token, label in exe_findings[:40]:
                print(f'    [{name}]  "{token}"  （{label}）')
        else:
            print('✔ 未发现疑似内部标识')

    print()
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

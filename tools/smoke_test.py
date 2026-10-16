"""
端到端冒烟测试：扫描 → OCR → 命名 → 复制 → 重名 → 撤销。

用法：
    python tools/smoke_test.py            # 自动生成测试数据并跑完整流程
    python tools/smoke_test.py --no-ocr   # 跳过真实 OCR（无引擎时）

会真实复制文件到 ./_testdata/_output，并在结束时打印结果表。
**不会触碰源文件**（源目录在测试前后做哈希对比）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (ExtractRule, NamingConfig, OcrConfig, OutputConfig, Status,
                    TaskConfig)
from core.task_manager import TaskManager
from database.database import Database
from utils.logger import TaskLogger


def tree_fingerprint(root: str) -> str:
    """整棵目录树的指纹（验证源数据未被修改）。"""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace('\\', '/')
            digest.update(rel.encode('utf-8'))
            try:
                with open(path, 'rb') as handle:
                    digest.update(hashlib.sha256(handle.read()).digest())
            except OSError:
                digest.update(b'<unreadable>')
    return digest.hexdigest()


def build_config(root: str, output: str, template: str) -> TaskConfig:
    return TaskConfig(
        rule=ExtractRule(root_dir=root, mode='advanced', template=template),
        naming=NamingConfig(fields=['姓名'], keep_source=True, source_position='prefix',
                            ai_enabled=True, ai_min_confidence=0.55,
                            fallback_to_source=True),
        ocr=OcrConfig(engine='auto', preprocess='standard'),
        output=OutputConfig(output_dir=output, conflict_policy='rename',
                            report_formats=['csv', 'json']),
    )


def print_table(records) -> None:
    print()
    print(f'{"序号":<4}{"来源文件夹":<14}{"OCR":<14}{"识别名":<12}{"最终文件名":<34}{"状态":<10}')
    print('-' * 96)
    for record in records:
        ocr_brief = (record.ocr_text or '').replace('\n', ' ').replace('|', ' ')[:12]
        print(f'{record.index:<4}{record.source_folder:<14}'
              f'{ocr_brief:<14}'
              f'{(record.extracted_name or "-")[:10]:<12}'
              f'{(record.final_filename or "-")[:32]:<34}'
              f'{Status.label(record.status):<10}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--template', default='*/1440/SAMPLE_NA.jpg')
    parser.add_argument('--no-ocr', action='store_true')
    parser.add_argument('--keep', action='store_true', help='保留输出目录')
    args = parser.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_root = os.path.join(base, '_testdata')
    output = os.path.join(data_root, '_output')

    # 1) 测试数据
    from tools.make_test_data import build
    source_root = build(data_root, with_images=not args.no_ocr)
    before = tree_fingerprint(source_root)
    print(f'[1/7] 测试数据：{source_root}')

    if os.path.isdir(output) and not args.keep:
        shutil.rmtree(output, ignore_errors=True)

    # 2) 扫描（只读）
    config = build_config(source_root, output, args.template)
    db = Database(os.path.join(data_root, '_test.db'))
    task_logger = TaskLogger('smoke', echo=None)
    manager = TaskManager(config, db, task_logger)

    scan_result = manager.scan()
    print(f'[2/7] 扫描：命中 {scan_result.count} 个文件，'
          f'访问目录 {scan_result.scanned_dirs} 个，耗时 {scan_result.elapsed:.3f}s')
    for record in scan_result.files:
        print(f'        {record.rel_path}')
    assert tree_fingerprint(source_root) == before, '扫描阶段不应改动源文件！'

    # 3) OCR + 命名（只读）
    if not args.no_ocr:
        summary = manager.run_ocr()
        print(f'[3/7] OCR：成功 {summary.ocr_ok}，失败 {summary.ocr_failed}，'
              f'需人工确认 {summary.needs_review}，耗时 {summary.elapsed_text}')
    else:
        for record in manager.records:
            record.extracted_name = '测试名'
            record.extract_method = 'manual'
            record.status = Status.READY
        print('[3/7] 跳过 OCR（--no-ocr）')

    assert tree_fingerprint(source_root) == before, 'OCR 阶段不应改动源文件！'
    print_table(manager.records)

    # 4) 命名分配
    stats = manager.rebuild_names()
    print(f'[4/7] 命名：分配 {stats["total"]} 个，重名改名 {stats["renamed"]} 个')

    # 5) 执行复制
    manager.save_to_db('ready')
    events = []
    result = manager.execute(progress_cb=lambda event: events.append(event) or True,
                             include_unreviewed=True)
    print(f'[5/7] 执行：成功 {result.summary.success}，失败 {result.summary.failed}，'
          f'跳过 {result.summary.skipped}，耗时 {result.summary.elapsed_text}')
    for path, error in result.errors:
        if error:
            print(f'        错误：{os.path.basename(path)} -> {error}')

    # 6) 验证：源文件没变、目标文件存在
    assert tree_fingerprint(source_root) == before, \
        '致命错误：执行阶段改动了源文件！'
    print('[6/7] 源目录指纹校验通过（原始文件零改动）')

    created = db.created_files(manager.task_id)
    print(f'        目标目录实际新增：{len(created)} 个文件')
    for path in sorted(os.listdir(output)) if os.path.isdir(output) else []:
        if path != '_reports':
            print(f'        {path}')

    # 6.5) 再跑一次完全相同的任务，验证 -1/-2 冲突处理
    if os.path.isdir(output):
        manager2 = TaskManager(build_config(source_root, output, args.template),
                               Database(os.path.join(data_root, '_test2.db')))
        manager2.scan()
        if not args.no_ocr:
            manager2.run_ocr()
        else:
            for record in manager2.records:
                record.extracted_name = '测试名'
                record.status = Status.READY
        manager2.rebuild_names()
        result2 = manager2.execute(include_unreviewed=True)
        print(f'        第二次执行（重名测试）：成功 {result2.summary.success}，'
              f'冲突改名 {result2.summary.conflicts}')
        names = [r.final_filename for r in manager2.records
                 if r.status == Status.SUCCESS]
        print('        第二次生成：' + ', '.join(names[:8]))
        assert result2.summary.conflicts >= 1, '重名文件应被改名而不是覆盖！'

    # 7) 撤销
    undo = manager.undo()
    print(f'[7/7] 撤销：删除 {undo["deleted"]}，跳过 {undo["skipped"]}，'
          f'失败 {undo["failed"]}')
    assert tree_fingerprint(source_root) == before, '撤销阶段不应改动源文件！'

    manager.close()
    print('\n✅ 冒烟测试全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

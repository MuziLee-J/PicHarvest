"""
SQLite 任务历史与明细存储（纯标准库）。

表结构：
    tasks        —— 每次任务一条汇总记录
    task_files   —— 每个文件的完整明细（含撤销所需的 created 标记）

并发：UI 主线程与工作线程都会写库，因此使用单连接 + 可重入锁，
并开启 WAL 模式提升并发读写稳定性。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from utils import file_utils
from utils.logger import get_logger

logger = get_logger('db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    root_dir     TEXT,
    template     TEXT,
    output_dir   TEXT,
    naming_fields TEXT,
    total        INTEGER DEFAULT 0,
    success      INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    needs_review INTEGER DEFAULT 0,
    status       TEXT,
    config_json  TEXT,
    log_path     TEXT,
    report_paths TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS task_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,
    seq             INTEGER,
    source_path     TEXT,
    rel_path        TEXT,
    source_folder   TEXT,
    original_name   TEXT,
    size            INTEGER,
    mtime           REAL,
    ocr_text        TEXT,
    ocr_engine      TEXT,
    ocr_confidence  REAL,
    ocr_error       TEXT,
    extracted_name  TEXT,
    extract_method  TEXT,
    extract_reason  TEXT,
    suggested_name  TEXT,
    final_filename  TEXT,
    status          TEXT,
    error           TEXT,
    conflict        INTEGER DEFAULT 0,
    conflict_note   TEXT,
    dest_path       TEXT,
    created         INTEGER DEFAULT 0,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_task ON task_files(task_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON task_files(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""

FILE_COLUMNS = (
    'task_id', 'seq', 'source_path', 'rel_path', 'source_folder', 'original_name',
    'size', 'mtime', 'ocr_text', 'ocr_engine', 'ocr_confidence', 'ocr_error',
    'extracted_name', 'extract_method', 'extract_reason', 'suggested_name',
    'final_filename', 'status', 'error', 'conflict', 'conflict_note',
    'dest_path', 'created',
)


def now_text() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


class Database:
    """任务历史数据库。"""

    def __init__(self, path: str | None = None):
        self.path = path or file_utils.get_db_path()
        self._lock = threading.RLock()
        file_utils.ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA foreign_keys=ON')
        except sqlite3.Error:
            pass
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -------------------------------------------------- 任务

    def create_task(self, root_dir: str, template: str, output_dir: str,
                    naming_fields: list | None = None,
                    config: dict | None = None, log_path: str = '') -> int:
        """新建任务，返回 task_id。"""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO tasks (created_at, root_dir, template, output_dir,
                       naming_fields, status, config_json, log_path)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now_text(), root_dir, template, output_dir,
                 ','.join(naming_fields or []), 'created',
                 json.dumps(config or {}, ensure_ascii=False), log_path))
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_task(self, task_id: int, **fields) -> None:
        """更新任务字段。"""
        if not fields:
            return
        allowed = {'started_at', 'finished_at', 'total', 'success', 'failed',
                   'skipped', 'needs_review', 'status', 'note', 'report_paths',
                   'log_path', 'root_dir', 'template', 'output_dir', 'naming_fields'}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        if isinstance(data.get('report_paths'), (list, tuple)):
            data['report_paths'] = json.dumps(list(data['report_paths']), ensure_ascii=False)
        assignments = ', '.join(f'{k}=?' for k in data)
        values = list(data.values()) + [task_id]
        with self._lock:
            self._conn.execute(f'UPDATE tasks SET {assignments} WHERE id=?', values)
            self._conn.commit()

    def mark_started(self, task_id: int) -> None:
        self.update_task(task_id, started_at=now_text(), status='running')

    def mark_finished(self, task_id: int, status: str = 'finished') -> None:
        self.update_task(task_id, finished_at=now_text(), status=status)

    # -------------------------------------------------- 明细

    def save_records(self, task_id: int, records: list, replace: bool = True) -> None:
        """写入/刷新文件明细（批量事务）。"""
        with self._lock:
            if replace:
                self._conn.execute('DELETE FROM task_files WHERE task_id=?', (task_id,))
            placeholders = ','.join('?' * len(FILE_COLUMNS))
            rows = [rec.to_db_tuple(task_id) for rec in records]
            self._conn.executemany(
                f'INSERT INTO task_files ({",".join(FILE_COLUMNS)}) VALUES ({placeholders})',
                rows)
            self._conn.commit()

    def update_record_status(self, task_id: int, seq: int, **fields) -> None:
        """更新单个文件的状态（执行阶段逐条落库，防止中途崩溃丢数据）。"""
        if not fields:
            return
        assignments = ', '.join(f'{k}=?' for k in fields)
        values = list(fields.values()) + [task_id, seq]
        with self._lock:
            self._conn.execute(
                f'UPDATE task_files SET {assignments} WHERE task_id=? AND seq=?', values)
            self._conn.commit()

    # -------------------------------------------------- 查询

    def list_tasks(self, limit: int = 200, keyword: str = '') -> list[dict]:
        """任务列表（按时间倒序）。"""
        sql = 'SELECT * FROM tasks'
        params: list[Any] = []
        if keyword:
            sql += (' WHERE root_dir LIKE ? OR template LIKE ? OR output_dir LIKE ?'
                    ' OR naming_fields LIKE ?')
            like = f'%{keyword}%'
            params = [like, like, like, like]
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def get_records(self, task_id: int, status: str = '') -> list[dict]:
        sql = 'SELECT * FROM task_files WHERE task_id=?'
        params: list[Any] = [task_id]
        if status:
            sql += ' AND status=?'
            params.append(status)
        sql += ' ORDER BY seq'
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def created_files(self, task_id: int) -> list[dict]:
        """本任务实际创建的文件（撤销用）。"""
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM task_files WHERE task_id=? AND created=1 AND status=?',
                (task_id, 'success')).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        """总体统计。"""
        with self._lock:
            total_tasks = self._conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            agg = self._conn.execute(
                'SELECT COALESCE(SUM(success),0), COALESCE(SUM(failed),0),'
                ' COALESCE(SUM(total),0) FROM tasks').fetchone()
        return {'tasks': int(total_tasks), 'success': int(agg[0]),
                'failed': int(agg[1]), 'files': int(agg[2])}

    # -------------------------------------------------- 维护

    def delete_task(self, task_id: int) -> None:
        with self._lock:
            self._conn.execute('DELETE FROM task_files WHERE task_id=?', (task_id,))
            self._conn.execute('DELETE FROM tasks WHERE id=?', (task_id,))
            self._conn.commit()

    def vacuum(self) -> None:
        with self._lock:
            try:
                self._conn.execute('VACUUM')
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass


_instance: Database | None = None
_instance_lock = threading.Lock()


def get_database(path: str | None = None) -> Database:
    """进程内单例。"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Database(path)
        return _instance

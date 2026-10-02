"""SQLite 作业存储。

作业状态机：queued -> processing -> completed / failed。
审计 JSON 与输出哈希直接落库（completed 时还保留 ZIP 文件供下载）。
所有写入串行化（单进程 uvicorn worker 下足够，WAL 保证读不阻塞）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import DB_PATH, ensure_dirs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    policy_id TEXT,
    input_sha256 TEXT,
    output_sha256 TEXT,
    output_size INTEGER,
    instances_in INTEGER DEFAULT 0,
    instances_out INTEGER DEFAULT 0,
    studies INTEGER DEFAULT 0,
    series INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    warning_count INTEGER DEFAULT 0,
    audit_json TEXT
);
"""


class Database:
    def __init__(self, path: Path = DB_PATH):
        ensure_dirs()
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def new_job_id() -> str:
        return uuid.uuid4().hex

    def create_job(self, job_id: str, policy_id: Optional[str],
                   input_sha256: Optional[str], created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, status, created_at, updated_at, "
                "policy_id, input_sha256) VALUES (?,?,?,?,?,?)",
                (job_id, "queued", created_at, created_at, policy_id,
                 input_sha256),
            )

    def mark_processing(self, job_id: str, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
                (ts, job_id),
            )

    def finish_job(self, job_id: str, *, status: str, ts: str,
                   instances_in: int, instances_out: int, studies: int,
                   series: int, error_count: int, warning_count: int,
                   output_sha256: Optional[str], output_size: Optional[int],
                   audit: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, instances_in=?, "
                "instances_out=?, studies=?, series=?, error_count=?, "
                "warning_count=?, output_sha256=?, output_size=?, "
                "audit_json=? WHERE id=?",
                (status, ts, instances_in, instances_out, studies, series,
                 error_count, warning_count, output_sha256, output_size,
                 json.dumps(audit, ensure_ascii=False), job_id),
            )

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["audit"] = json.loads(data.pop("audit_json")) \
            if data.get("audit_json") else None
        return data

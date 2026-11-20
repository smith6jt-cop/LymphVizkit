"""SQLite access: one connection per thread, ordered SQL migrations tracked by user_version."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """Thin wrapper. Writes are serialised by one process-wide lock (single uvicorn worker)."""

    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._local = threading.local()
        self.write_lock = threading.RLock()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.read_only:
                c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            else:
                c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # ---- helpers -------------------------------------------------------------
    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.write_lock:
            c = self.conn()
            cur = c.execute(sql, params)
            c.commit()
            return cur

    def transaction(self):
        return _Tx(self)

    def audit(self, user_id: str, action: str, **detail) -> None:
        self.execute(
            "INSERT INTO audit_log(ts, user_id, action, detail) VALUES (?,?,?,?)",
            (utcnow(), user_id, action, json.dumps(detail, sort_keys=True, default=str)),
        )

    # ---- migrations ----------------------------------------------------------
    def migrate(self, journal_mode: str = "WAL") -> int:
        """Apply pending migrations in filename order. Returns the schema version."""
        c = self.conn()
        # WAL is not safe on network filesystems; LYMPH_FLOW_SQLITE_JOURNAL=DELETE falls back.
        c.execute(f"PRAGMA journal_mode={journal_mode}")
        c.execute("PRAGMA synchronous=NORMAL")
        current = c.execute("PRAGMA user_version").fetchone()[0]
        files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        for f in files:
            version = int(f.name[:3])
            if version <= current:
                continue
            with self.write_lock:
                c.executescript("BEGIN;\n" + f.read_text() + f"\nPRAGMA user_version={version};\nCOMMIT;")
            current = version
        return current


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db.write_lock.acquire()
        self.c = self.db.conn()
        return self.c

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.c.commit()
            else:
                self.c.rollback()
        finally:
            self.db.write_lock.release()
        return False

"""
cache.py — Simple SQLite result cache for verify/test evaluations.

Keys = SHA256 of: task_id + normalized candidate source + config hash.
Safe for concurrent workers (WAL mode).

Usage:
    from equiv.cache import EvalCache
    cache = EvalCache("data/cache.db")
    if result := cache.get(task_id, candidate_src, config_hash):
        ...
    cache.put(task_id, candidate_src, config_hash, result_dict)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class EvalCache:
    """SQLite-backed cache for evaluation results."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS eval_cache (
                    key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    def _make_key(self, task_id: str, candidate_src: str, config_hash: str) -> str:
        """Create a cache key from task + candidate + config."""
        payload = f"{task_id}\n{candidate_src}\n{config_hash}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, task_id: str, candidate_src: str, config_hash: str) -> dict | None:
        """Retrieve a cached result, or None if not found."""
        key = self._make_key(task_id, candidate_src, config_hash)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT result_json FROM eval_cache WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row[0])
        return None

    def put(self, task_id: str, candidate_src: str, config_hash: str,
            result: dict) -> None:
        """Store a result in the cache."""
        key = self._make_key(task_id, candidate_src, config_hash)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO eval_cache (key, result_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(result), __import__("time").time()),
        )
        conn.commit()

    def stats(self) -> dict:
        """Return cache statistics."""
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM eval_cache").fetchone()[0]
        return {"entries": count, "db_path": str(self.db_path)}

    def close(self):
        """Close the connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
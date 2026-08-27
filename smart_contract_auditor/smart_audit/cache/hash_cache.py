"""
hash_cache.py
Local SQLite cache keyed on sha256(contract_source + finding_id). Identical
local runs (same contract, same Slither finding) bypass the LLM courtroom
pipeline entirely -- straight cache hit, zero budget consumed.

Permanent cache, no TTL: a given (contract_source, finding_id) pair always
maps to the same code slice, so a past verdict stays valid until the source
changes -- at which point the hash itself changes naturally.
"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("output") / "hash_cache.db"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_entries (
            hash        TEXT PRIMARY KEY,
            verdict     TEXT NOT NULL,
            created_at  REAL NOT NULL
        )
        """
    )
    conn.commit()


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        _init_db(conn)
        yield conn
    finally:
        conn.close()


def compute_hash(contract_source: str, finding_id: str) -> str:
    """sha256(contract_source + finding_id), hex digest."""
    payload = (contract_source + finding_id).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get(contract_source: str, finding_id: str) -> dict | None:
    """Returns the cached verdict dict, or None on a cache miss."""
    key = compute_hash(contract_source, finding_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT verdict FROM cache_entries WHERE hash = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set(contract_source: str, finding_id: str, verdict: dict) -> None:
    """Stores/overwrites the verdict for this (contract_source, finding_id) pair."""
    key = compute_hash(contract_source, finding_id)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO cache_entries (hash, verdict, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET verdict = excluded.verdict,
                                             created_at = excluded.created_at
            """,
            (key, json.dumps(verdict), time.time()),
        )
        conn.commit()


def clear() -> None:
    """Wipes the entire cache. Useful for --no-cache / forced re-audit runs."""
    with _connect() as conn:
        conn.execute("DELETE FROM cache_entries")
        conn.commit()
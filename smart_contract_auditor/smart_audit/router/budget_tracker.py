"""
budget_tracker.py
SQLite-backed local quota tracker. Single source of truth for provider state.
No shared object passed between agents -- api_router.py calls these functions
fresh each time, state lives on disk.

Two check modes:
  - check_and_increment(): HARD limit, atomic reserve. Never exceeds cap.
  - is_near_limit() / remaining_ratio(): SOFT headroom check, read-only.
    Router uses this to proactively hop to next provider tier mid-contract,
    before actually hitting the hard wall (avoids 429s + mid-run tier flips).
"""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("output") / "budget_tracker.db"

# provider limits -- edit here if free-tier terms change
# rpm_limit / daily_limit / token_limit = None means "not enforced"
PROVIDER_LIMITS = {
    "nvidia": {
        "rpm_limit": 40,
        "daily_limit": None,
        "token_limit": None,      # no published cap; slices are small anyway
        "token_window": "daily",
    },
    "mistral": {
        "rpm_limit": None,        # not published, rely on token allowance
        "daily_limit": None,
        "token_limit": 1_000_000_000,
        "token_window": "monthly",
    },
    "openrouter": {
        "rpm_limit": 20,
        "daily_limit": 50,        # conservative floor of 50-1000 range
        "token_limit": None,
        "token_window": "daily",
    },
}

RPM_WINDOW_SECONDS = 60
SOFT_THRESHOLD = 0.85  # switch provider once usage crosses 85% of any capped dim


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_state (
            provider          TEXT PRIMARY KEY,
            rpm_count         INTEGER NOT NULL DEFAULT 0,
            rpm_window_start  REAL    NOT NULL DEFAULT 0,
            daily_count       INTEGER NOT NULL DEFAULT 0,
            daily_window_date TEXT    NOT NULL DEFAULT '',
            token_count       INTEGER NOT NULL DEFAULT 0,
            token_window_key  TEXT    NOT NULL DEFAULT '',
            last_used         REAL    NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        _init_db(conn)
        yield conn
    finally:
        conn.close()


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _get_or_create_row(conn: sqlite3.Connection, provider: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM provider_state WHERE provider = ?", (provider,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO provider_state (provider) VALUES (?)", (provider,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM provider_state WHERE provider = ?", (provider,)
        ).fetchone()
    return row


def _resolve_window(provider: str, row: sqlite3.Row) -> dict:
    """
    Computes current counts, applying window resets in-memory (no persist).
    Shared by the hard-check (which persists after) and the soft read-only checks.
    """
    limits = PROVIDER_LIMITS[provider]
    now = time.time()
    today = _today_key()
    token_window_key = _month_key() if limits["token_window"] == "monthly" else today

    rpm_count = row["rpm_count"]
    rpm_window_start = row["rpm_window_start"]
    if now - rpm_window_start >= RPM_WINDOW_SECONDS:
        rpm_count = 0
        rpm_window_start = now

    daily_count = row["daily_count"]
    daily_window_date = row["daily_window_date"]
    if daily_window_date != today:
        daily_count = 0
        daily_window_date = today

    token_count = row["token_count"]
    token_window_key_stored = row["token_window_key"]
    if token_window_key_stored != token_window_key:
        token_count = 0
        token_window_key_stored = token_window_key

    return {
        "now": now,
        "rpm_count": rpm_count,
        "rpm_window_start": rpm_window_start,
        "daily_count": daily_count,
        "daily_window_date": daily_window_date,
        "token_count": token_count,
        "token_window_key": token_window_key_stored,
    }


def check_and_increment(provider: str, estimated_tokens: int = 0) -> bool:
    """
    Atomically checks rpm/daily/token budget for `provider`. If under limit,
    increments counters and returns True. If over limit, does NOT increment
    and returns False (caller should fall through to next provider tier).
    This is the HARD boundary -- never exceeded.
    """
    if provider not in PROVIDER_LIMITS:
        raise ValueError(f"unknown provider: {provider}")
    limits = PROVIDER_LIMITS[provider]

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get_or_create_row(conn, provider)
        w = _resolve_window(provider, row)

        if limits["rpm_limit"] is not None and w["rpm_count"] + 1 > limits["rpm_limit"]:
            conn.execute("COMMIT")
            return False
        if limits["daily_limit"] is not None and w["daily_count"] + 1 > limits["daily_limit"]:
            conn.execute("COMMIT")
            return False
        if (
            limits["token_limit"] is not None
            and w["token_count"] + estimated_tokens > limits["token_limit"]
        ):
            conn.execute("COMMIT")
            return False

        conn.execute(
            """
            UPDATE provider_state
            SET rpm_count = ?, rpm_window_start = ?,
                daily_count = ?, daily_window_date = ?,
                token_count = ?, token_window_key = ?,
                last_used = ?
            WHERE provider = ?
            """,
            (
                w["rpm_count"] + 1,
                w["rpm_window_start"],
                w["daily_count"] + 1,
                w["daily_window_date"],
                w["token_count"] + estimated_tokens,
                w["token_window_key"],
                w["now"],
                provider,
            ),
        )
        conn.commit()
        return True


def remaining_ratio(provider: str) -> dict:
    """
    Read-only. Returns fraction of capacity REMAINING per dimension (1.0 = full,
    0.0 = exhausted). None for dims with no configured limit (unenforced).
    """
    if provider not in PROVIDER_LIMITS:
        raise ValueError(f"unknown provider: {provider}")
    limits = PROVIDER_LIMITS[provider]

    with _connect() as conn:
        row = _get_or_create_row(conn, provider)
        w = _resolve_window(provider, row)

    def _ratio(used, limit):
        if limit is None:
            return None
        return max(0.0, 1.0 - (used / limit))

    return {
        "rpm": _ratio(w["rpm_count"], limits["rpm_limit"]),
        "daily": _ratio(w["daily_count"], limits["daily_limit"]),
        "token": _ratio(w["token_count"], limits["token_limit"]),
    }


def is_near_limit(provider: str, threshold: float = SOFT_THRESHOLD) -> bool:
    """
    Soft check -- True if ANY capped dimension has used >= threshold fraction
    of its budget (i.e. remaining <= 1 - threshold). Unenforced dims (None)
    never trigger this. Router calls this BEFORE dispatching to decide whether
    to proactively hop to the next tier, rather than waiting for a hard 429.
    """
    ratios = remaining_ratio(provider)
    return any(r is not None and r <= (1.0 - threshold) for r in ratios.values())


def get_status(provider: str) -> dict:
    """Read-only snapshot of current counters + remaining headroom (for CLI display)."""
    with _connect() as conn:
        row = _get_or_create_row(conn, provider)
        w = _resolve_window(provider, row)
    return {
        "provider": provider,
        "rpm_count": w["rpm_count"],
        "daily_count": w["daily_count"],
        "token_count": w["token_count"],
        "limits": PROVIDER_LIMITS[provider],
        "remaining_ratio": remaining_ratio(provider),
        "near_limit": is_near_limit(provider),
    }


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token. Good enough for soft-cap checks."""
    return max(1, len(text) // 4)
"""SQLite access layer for the POS assistant database.

All PHASE 3 mitigations live here so the query functions in ``service.py`` stay
clean:

* Concurrency (single-writer lock): WAL journal mode (readers never block the
  writer), a ``busy_timeout`` so contended calls *wait* instead of raising
  ``database is locked``, read connections pinned to ``query_only`` so a metric
  can never accidentally write, and ``BEGIN IMMEDIATE`` for writes so the lock
  is taken up front and contention surfaces immediately (and gracefully).
* Date/Time: ``now_local`` / ``resolve_period`` anchor every relative timeframe
  to one tz-aware "now" in Python. SQLite's ``date('now')`` is UTC and would
  drift ±6 h for Costa Rica near midnight, so we never use it.
* Text matching: a registered ``unaccent()`` SQL function (NFKD strip +
  lowercase) folds accents/case so ``María`` == ``maria`` == ``MARIA``;
  ``like_term`` escapes wildcards so user input can't become "match anything".
"""
import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Default DB path: <project root>/pos_assistant.db. Overridable via env (and, in
# tests, by monkeypatching this module attribute — connections read it lazily).
DB_PATH = Path(
    os.environ.get("POS_DB_PATH", Path(__file__).resolve().parents[2] / "pos_assistant.db")
)

# The timezone the DB timestamps are written in. MUST match how rows are stored
# (create_database.py uses naive local time); anchoring reads in a different
# zone is the classic off-by-6-hours "today" bug.
POS_TZ = ZoneInfo(os.environ.get("POS_TZ", "America/Costa_Rica"))

BUSY_TIMEOUT_MS = 5000
_FMT = "%Y-%m-%d %H:%M:%S"


# ── time helpers (PHASE 3 #1) ─────────────────────────────────────────────────

def now_local() -> datetime:
    """The single source of truth for 'now' — tz-aware, never SQLite's UTC clock."""
    return datetime.now(POS_TZ)


def now_str() -> str:
    return now_local().strftime(_FMT)


def _first_of_next_month(d: datetime) -> datetime:
    return d.replace(year=d.year + 1, month=1, day=1) if d.month == 12 \
        else d.replace(month=d.month + 1, day=1)


def _first_of_prev_month(d: datetime) -> datetime:
    return d.replace(year=d.year - 1, month=12, day=1) if d.month == 1 \
        else d.replace(month=d.month - 1, day=1)


def resolve_period(period: str | None) -> tuple[str, str, str]:
    """Map a relative term to a half-open ``[start, end)`` of 'YYYY-MM-DD HH:MM:SS'.

    Returns ``(start, end, label)``. Unknown terms fall back to 'today' (safe
    default) rather than scanning the whole table. ``end`` is exclusive so we
    never double-count the boundary second.
    """
    now = now_local()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    p = (period or "today").strip().lower().replace(" ", "_")

    if p in ("today",):
        start, end = today, today + timedelta(days=1)
    elif p in ("yesterday",):
        start, end = today - timedelta(days=1), today
    elif p in ("this_week", "week"):
        start = today - timedelta(days=today.weekday())   # Monday
        end = start + timedelta(days=7)
    elif p in ("last_7_days", "past_week", "last_week"):
        start, end = today - timedelta(days=7), today + timedelta(days=1)
    elif p in ("this_month", "month"):
        start = today.replace(day=1)
        end = _first_of_next_month(start)
    elif p in ("last_month",):
        end = today.replace(day=1)
        start = _first_of_prev_month(end)
    elif p in ("last_30_days", "past_month"):
        start, end = today - timedelta(days=30), today + timedelta(days=1)
    elif p in ("all", "all_time", "ever"):
        start, end = datetime(1970, 1, 1, tzinfo=POS_TZ), today + timedelta(days=3650)
    else:
        p, start, end = "today", today, today + timedelta(days=1)

    return start.strftime(_FMT), end.strftime(_FMT), p


# ── text helpers (PHASE 3 #3) ─────────────────────────────────────────────────

def _unaccent(value) -> str | None:
    """Lowercase + strip diacritics. Registered as a SQLite UDF and used both
    sides of comparisons so 'María' folds to 'maria'."""
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def like_term(raw: str) -> str:
    """Escape LIKE wildcards in user input, then wrap as a contains-pattern.

    Prevents a name like ``%`` or ``a_b`` from turning a lookup into a wildcard
    scan. Pair with ``ESCAPE '\\'`` in the query.
    """
    escaped = (raw or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ── connections (PHASE 3 #2) ──────────────────────────────────────────────────

def _db_path() -> str:
    return str(DB_PATH)


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.create_function("unaccent", 1, _unaccent, deterministic=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")


def initialize() -> None:
    """Set WAL once (it persists in the DB header). Call at server startup.

    WAL lets readers proceed during the single writer's transaction — essential
    when many WhatsApp users read while one permission update is in flight.
    """
    with sqlite3.connect(_db_path(), timeout=BUSY_TIMEOUT_MS / 1000) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


@contextmanager
def read_connection():
    """A read-only connection. ``query_only`` makes accidental writes impossible
    and avoids ever taking the write lock, so metrics never contend."""
    conn = sqlite3.connect(_db_path(), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        _configure(conn)
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def write_connection():
    """A serialized write transaction.

    ``BEGIN IMMEDIATE`` grabs the write lock at the start (paired with
    ``busy_timeout`` it waits politely under contention); commit/rollback are
    explicit so a failed audit-log insert rolls back the permission change too.
    """
    conn = sqlite3.connect(_db_path(), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        _configure(conn)
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

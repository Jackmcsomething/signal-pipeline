"""
database.py
───────────
SQLite database for storing every event we see (whether or not we notify on it).

Why SQLite:
  - Zero setup, just a single file
  - Lives in the repo, gets versioned by git
  - Plenty fast for our volume (a few hundred events/day max)
  - When we add the v2 performance tracker, we just query this same db

Two tables:
  signals          - every event we've detected
  notifications    - which signals we've actually pushed (for dedup)

The dedup logic prevents you from getting the same alert twice if the pipeline
runs and re-detects an event (which happens often — RSS feeds re-publish, APIs
return overlapping windows, etc).
"""
import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

import config

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    """
    Context manager for database connections.

    Usage:
        with get_db() as conn:
            conn.execute("SELECT * FROM signals")

    Auto-commits on clean exit, rolls back on exception, always closes.
    """
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name, not index
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- unique identifier so we don't insert duplicates. Format depends on source:
    --   earnings:  "EARNINGS_<ticker>_<period_end_date>"
    --   ma_us:     "MA_US_<accession_number>"
    --   ma_uk:     "MA_UK_<rns_id>"
    event_id        TEXT UNIQUE NOT NULL,

    -- 'earnings' | 'ma_us' | 'ma_uk'
    source          TEXT NOT NULL,

    -- the stock that's the subject of the event (target for M&A)
    ticker          TEXT NOT NULL,
    company_name    TEXT,
    market          TEXT,                -- 'US' or 'UK'
    market_cap_usd  REAL,                -- normalized to USD for comparison

    -- event details (different fields used per source)
    event_time      TEXT NOT NULL,       -- ISO datetime when announced
    surprise_pct    REAL,                -- earnings: % beat/miss
    deal_size_usd   REAL,                -- M&A: deal value in USD
    deal_premium    REAL,                -- M&A: % premium over prior close

    -- our scoring
    score           INTEGER,             -- -1, 0, or +1
    is_high_conviction BOOLEAN DEFAULT 0,

    -- raw payload for debugging / future re-analysis
    raw_data        TEXT,                -- JSON blob

    -- audit
    detected_at     TEXT NOT NULL,       -- when our pipeline first saw it
    notified_at     TEXT                 -- when we sent the Pushover (NULL = not yet)
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source);
CREATE INDEX IF NOT EXISTS idx_signals_detected_at ON signals(detected_at);
"""


def init_db():
    """Create tables if they don't exist. Safe to call repeatedly."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
    log.info(f"Database initialized at {config.DATABASE_PATH}")


# ─────────────────────────────────────────────────────────────────────────
# Insert / dedup
# ─────────────────────────────────────────────────────────────────────────
def event_already_seen(event_id: str) -> bool:
    """Check if we've already inserted this event. Used for dedup."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM signals WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return row is not None


def insert_signal(signal: dict) -> int | None:
    """
    Insert a new signal. Returns the new row id, or None if it was a duplicate.

    Expected keys in `signal`:
        event_id, source, ticker, event_time
    Optional keys:
        company_name, market, market_cap_usd, surprise_pct, deal_size_usd,
        deal_premium, score, is_high_conviction, raw_data
    """
    if event_already_seen(signal["event_id"]):
        log.debug(f"Skipping duplicate event {signal['event_id']}")
        return None

    signal = {**signal, "detected_at": datetime.utcnow().isoformat()}

    columns = ", ".join(signal.keys())
    placeholders = ", ".join(["?"] * len(signal))
    sql = f"INSERT INTO signals ({columns}) VALUES ({placeholders})"

    with get_db() as conn:
        cursor = conn.execute(sql, tuple(signal.values()))
        return cursor.lastrowid


def mark_notified(event_id: str):
    """Record that we sent a Pushover notification for this event."""
    with get_db() as conn:
        conn.execute(
            "UPDATE signals SET notified_at = ? WHERE event_id = ?",
            (datetime.utcnow().isoformat(), event_id),
        )


def get_pending_notifications() -> list[dict]:
    """
    Return all signals that we should notify on but haven't yet.

    A signal is "pending notification" if:
      - it has been inserted into the DB
      - notified_at is NULL
      - it meets the notify thresholds (score != 0 OR explicitly high-conviction)
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signals
            WHERE notified_at IS NULL
              AND (score != 0 OR is_high_conviction = 1)
            ORDER BY detected_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]

"""
database.py
───────────
SQLite database for storing every event we see (whether or not we notify on it).

Why SQLite:
  - Zero setup, just a single file
  - Lives in the repo, gets versioned by git
  - Plenty fast for our volume (a few hundred events/day max)
  - When we add the v2 performance tracker, we just query this same db

Three tables:
  signals           - every event we've detected
  company_profiles  - cached Finnhub /stock/profile2 data (24h TTL)
                      prevents hammering the API every cycle for the same tickers

The dedup logic prevents you from getting the same alert twice if the pipeline
runs and re-detects an event (which happens often — RSS feeds re-publish, APIs
return overlapping windows, etc).
"""
from __future__ import annotations

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

-- ─────────────────────────────────────────────────────────────────────────
-- Company profile cache (Finnhub /stock/profile2)
-- ─────────────────────────────────────────────────────────────────────────
-- Avoids calling Finnhub for the same ticker every 5-minute cycle.
-- TTL is enforced in Python (see earnings.py CACHE_TTL_HOURS), not here.
-- INSERT OR REPLACE gives us a free upsert — last_updated is refreshed on
-- every successful Finnhub response.
CREATE TABLE IF NOT EXISTS company_profiles (
    ticker          TEXT PRIMARY KEY,
    company_name    TEXT,
    market_cap_usd  REAL,
    sector          TEXT,       -- finnhubIndustry e.g. "Technology"
    industry        TEXT,       -- GICS sub-industry (gsubind), may be NULL
    exchange        TEXT,       -- e.g. "NASDAQ", "LSE"
    country         TEXT,       -- e.g. "US", "GB"
    last_updated    TEXT NOT NULL   -- ISO UTC timestamp of last Finnhub fetch
);
"""


def _run_migrations(conn):
    """
    Add new columns to existing tables without touching existing data.

    ALTER TABLE ADD COLUMN is idempotent here: SQLite raises OperationalError
    with "duplicate column name" if the column already exists; we catch and
    ignore that specific error so this function is safe to call every cycle.

    Do NOT add CREATE TABLE statements here — those belong in SCHEMA above.
    """
    def _add_col(table: str, col_def: str):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise  # genuine schema error — let it bubble up

    # ── signals: v1 mirror columns (for paper-test comparison) ────────────
    _add_col("signals", "v1_score INTEGER")
    _add_col("signals", "v1_high_conviction BOOLEAN DEFAULT 0")
    _add_col("signals", "v1_would_notify BOOLEAN DEFAULT 0")

    # ── signals: v2 conviction scoring columns ────────────────────────────
    _add_col("signals", "v2_magnitude_score INTEGER")
    _add_col("signals", "v2_absolute_surprise_score INTEGER")
    _add_col("signals", "v2_absolute_surprise_usd REAL")
    _add_col("signals", "v2_absolute_surprise_method TEXT")
    _add_col("signals", "v2_cap_modifier INTEGER")
    _add_col("signals", "v2_conviction_score INTEGER")
    _add_col("signals", "v2_tier TEXT")
    _add_col("signals", "v2_would_notify BOOLEAN DEFAULT 0")
    _add_col("signals", "v2_reason_codes TEXT")   # JSON-serialised list

    # ── company_profiles: shares outstanding (needed for v2 abs surprise) ─
    _add_col("company_profiles", "shares_outstanding REAL")  # actual share count


def init_db():
    """Create tables and run schema migrations. Safe to call every cycle."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
    # executescript() issues an implicit COMMIT before running, so migrations
    # must run in a separate connection context after tables are guaranteed
    # to exist.
    with get_db() as conn:
        _run_migrations(conn)
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
    Return all signals that should be notified but haven't been yet.

    Notification decision differs by source:
      - earnings : use v2_would_notify (v2 conviction model)
      - ma_us / ma_uk : use v1 score != 0 (M&A stays on v1)

    Old earnings rows from before v2 was deployed have NULL v2_would_notify.
    Those rows are already marked notified_at (they fired under the old system),
    so the NULL check here is safe — they won't be re-triggered.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signals
            WHERE notified_at IS NULL
              AND (
                  (source = 'earnings' AND v2_would_notify = 1)
                  OR
                  (source IN ('ma_us', 'ma_uk')
                   AND (score != 0 OR is_high_conviction = 1))
              )
            ORDER BY detected_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Company profile cache
# ─────────────────────────────────────────────────────────────────────────

def get_cached_profile(ticker: str) -> dict | None:
    """
    Return the cached profile row for a ticker, or None if not cached.
    Does NOT check freshness — the caller decides whether to use or refresh it.
    Returned dict keys: ticker, company_name, market_cap_usd, sector,
                        industry, exchange, country, last_updated.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM company_profiles WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return dict(row) if row else None


def upsert_profile(ticker: str, profile: dict):
    """
    Insert or update a company profile. Overwrites on conflict (ticker is PK).
    Sets last_updated to now (UTC).

    Expected keys in profile (all optional except ticker):
        company_name, market_cap_usd, sector, industry, exchange, country,
        shares_outstanding (actual share count, not millions)
    """
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO company_profiles
                (ticker, company_name, market_cap_usd, sector, industry,
                 exchange, country, shares_outstanding, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                profile.get("company_name"),
                profile.get("market_cap_usd"),
                profile.get("sector"),
                profile.get("industry"),
                profile.get("exchange"),
                profile.get("country"),
                profile.get("shares_outstanding"),
                now,
            ),
        )

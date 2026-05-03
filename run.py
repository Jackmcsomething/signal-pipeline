"""
run.py
──────
Main entry point. One execution = one pipeline cycle:

  1. Init database (idempotent — safe to call every run)
  2. Check if we're in active hours; if not, exit
  3. Fetch events from all three sources (earnings, US M&A, UK M&A)
  4. Score each event
  5. Persist to DB (auto-dedups based on event_id)
  6. Find pending notifications (signals not yet pushed)
  7. For each, generate AI take + send Pushover
  8. Mark as notified

Run locally:    python run.py
Run on Actions: triggered by .github/workflows/pipeline.yml every 5 mins
"""
import os
import sys
import logging
from datetime import datetime

# Make src importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env file if running locally (no-op on GitHub Actions)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed → skip

import config
from src import database, earnings, ma_us, ma_uk, scoring, ai_take, notify


# ─────────────────────────────────────────────────────────────────────────
# Logging setup — verbose so the GitHub Actions log is useful
# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pipeline")


def is_active_hour() -> bool:
    """Skip runs outside of active hours to save API quota."""
    now_utc = datetime.utcnow()
    hour = now_utc.hour
    weekday = now_utc.weekday()  # 0=Mon, 6=Sun

    if weekday >= 5:  # Sat or Sun
        log.info(f"Weekend (weekday={weekday}), skipping")
        return False

    if hour < config.ACTIVE_HOURS_UTC_START or hour >= config.ACTIVE_HOURS_UTC_END:
        log.info(f"Outside active hours (hour={hour} UTC), skipping")
        return False

    return True


def run_cycle():
    """One full pipeline iteration."""
    log.info("=" * 60)
    log.info(f"Pipeline cycle starting at {datetime.utcnow().isoformat()}")
    log.info("=" * 60)

    # 1. Init DB
    database.init_db()

    # 2. Active hours check
    if not is_active_hour():
        return

    # 3. Fetch from all sources, isolating failures
    all_events = []

    log.info("─── EARNINGS ───")
    try:
        all_events.extend(earnings.fetch_recent_earnings())
    except Exception as e:
        log.error(f"Earnings fetch failed: {e}", exc_info=True)

    log.info("─── US M&A ───")
    try:
        all_events.extend(ma_us.fetch_us_ma())
    except Exception as e:
        log.error(f"US M&A fetch failed: {e}", exc_info=True)

    log.info("─── UK M&A ───")
    try:
        all_events.extend(ma_uk.fetch_uk_ma())
    except Exception as e:
        log.error(f"UK M&A fetch failed: {e}", exc_info=True)

    log.info(f"Total events fetched: {len(all_events)}")

    # 4 + 5. Score and persist
    # Earnings events come pre-scored (v1 + v2) from fetch_recent_earnings().
    # M&A events still use v1 scoring here — M&A scoring is unchanged.
    new_count = 0
    for event in all_events:
        if event["source"] != "earnings":
            scoring.score_event(event)   # v1 only for M&A
        row_id = database.insert_signal(event)
        if row_id is not None:
            new_count += 1
    log.info(f"New events persisted: {new_count}")

    # 6 + 7 + 8. Notify
    pending = database.get_pending_notifications()
    log.info(f"Pending notifications: {len(pending)}")

    for event in pending:
        try:
            take = ai_take.get_ai_take(event)
            success = notify.send_pushover(event, take)
            if success:
                database.mark_notified(event["event_id"])
        except Exception as e:
            log.error(f"Notify failed for {event.get('event_id')}: {e}", exc_info=True)

    log.info("Pipeline cycle complete")


if __name__ == "__main__":
    run_cycle()

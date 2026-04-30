"""
worker.py
─────────
Railway entry point. Runs the pipeline in an infinite loop, sleeping
POLL_INTERVAL_MINUTES between cycles.

Why a long-running worker instead of Railway's cron:
  Railway's cron spins up a new container per run (cold start ~10–15s, then
  pip install on every tick). A persistent worker stays warm, uses less
  resource, and gives us cleaner logs in one continuous stream.

Graceful shutdown:
  Railway sends SIGTERM before killing a container (e.g. on deploy or
  restart). We catch it, finish the current sleep or cycle, then exit
  cleanly. This prevents a half-written DB state on restart.

If run.py raises an unhandled exception, we log it, wait one interval,
then continue — so a single bad cycle (e.g. Finnhub API blip) doesn't
take down the whole worker.
"""
import signal
import time
import logging
import sys
import os

# Make project root importable (same pattern as run.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env if running locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
from run import run_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")


# ─────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    """
    Railway sends SIGTERM before SIGKILL (default grace period: 10s).
    We flip the flag here; the main loop checks it before each sleep
    and exits cleanly rather than mid-cycle.
    """
    global _shutdown_requested
    log.info("SIGTERM received — will shut down after current cycle")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)   # also handles Ctrl-C locally


# ─────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────
def main():
    interval = config.POLL_INTERVAL_MINUTES * 60  # seconds

    log.info("=" * 60)
    log.info("Signal Pipeline worker starting")
    log.info(f"Poll interval: {config.POLL_INTERVAL_MINUTES} minutes")
    log.info(f"Active hours (UTC): {config.ACTIVE_HOURS_UTC_START}:00 – {config.ACTIVE_HOURS_UTC_END}:00")
    log.info("=" * 60)

    while not _shutdown_requested:
        try:
            run_cycle()
        except Exception as e:
            # Log the error but keep the worker alive — a transient API
            # failure shouldn't kill the process. Railway's restart policy
            # handles true crashes (OOM, unrecoverable state, etc).
            log.error(f"Unhandled exception in run_cycle: {e}", exc_info=True)

        if _shutdown_requested:
            break

        log.info(f"Sleeping {config.POLL_INTERVAL_MINUTES} minutes until next cycle")

        # Sleep in 1-second ticks so SIGTERM is picked up promptly
        # rather than blocking for the full 5-minute interval.
        for _ in range(interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    log.info("Worker shut down cleanly")


if __name__ == "__main__":
    main()

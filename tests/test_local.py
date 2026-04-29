"""
tests/test_local.py
───────────────────
Run this BEFORE deploying to GitHub Actions to confirm everything works.

What it does:
  1. Loads your .env file
  2. Sends a test Pushover notification (confirms Pushover is wired up)
  3. Runs one full pipeline cycle (confirms data fetches + DB work)
  4. Reports what it found

If you get a notification on your phone with the title "📈 BEAT 12.5% │ TEST",
your Pushover keys are correct.

If the cycle prints earnings or M&A events, the data sources are working.
"""
import os
import sys
import logging

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed. Run: pip install python-dotenv")
    print("    (or set env vars manually)")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

from src import notify
from run import run_cycle


def check_env():
    """Confirm all required env vars are present."""
    required = [
        "FINNHUB_API_KEY",
        "PUSHOVER_USER_KEY",
        "PUSHOVER_APP_TOKEN",
        "ANTHROPIC_API_KEY",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}")
        print("   Add them to .env and try again.")
        return False
    print("✅ All env vars present")
    return True


def main():
    print("\n" + "=" * 60)
    print("  Signal Pipeline — local test")
    print("=" * 60 + "\n")

    if not check_env():
        sys.exit(1)

    print("\n▶ Sending test Pushover notification...")
    if notify.send_test_notification():
        print("✅ Test notification sent — check your phone")
    else:
        print("❌ Test notification failed — check Pushover keys")
        sys.exit(1)

    print("\n▶ Running one full pipeline cycle...")
    print("   (this fetches from all three sources)\n")
    run_cycle()

    print("\n" + "=" * 60)
    print("  Done. Check signals.db to see what was logged.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

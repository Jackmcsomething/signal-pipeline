"""
tests/test_local.py
───────────────────
Run this BEFORE deploying to confirm everything works.

What it does:
  1. test_profile_cache() — unit test, no API keys needed. Proves the SQLite
     profile cache works: first call hits Finnhub, second call within 24h
     returns cached data without an API call.
  2. check_env()         — confirms all 4 required env vars are present.
  3. send_test_notification() — sends a live Pushover ping to your phone.
  4. run_cycle()         — one full pipeline cycle across all three sources.

If you get a notification titled "📈 BEAT 12.5% │ TEST", Pushover is wired up.
If the cycle logs earnings or M&A events, the data sources are working.
"""
import os
import sys
import logging
from unittest.mock import patch

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed. Run: pip install python-dotenv")
    print("    (or set env vars manually)")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

from src import notify, database
from src.earnings import _get_company_profile, CACHE_TTL_HOURS
from run import run_cycle


# ─────────────────────────────────────────────────────────────────────────
# 1. Profile cache unit test  (no API key required)
# ─────────────────────────────────────────────────────────────────────────

def test_profile_cache():
    """
    Verify that the company profile cache works end-to-end:

      First call  → cache miss → Finnhub API called → result written to SQLite
      Second call → cache hit  → Finnhub API NOT called → data from SQLite

    Uses unittest.mock so no real Finnhub key is required.
    Cleans up the test row before and after so it doesn't pollute real data.
    """
    print("\n▶ Testing profile cache (no API key required)...")

    # Init DB so the company_profiles table exists
    database.init_db()

    # Use an obviously fake ticker that will never appear in real data
    test_ticker = "_TEST_CACHE_TICKER_"

    # Clean up any leftover from a previous run
    with database.get_db() as conn:
        conn.execute(
            "DELETE FROM company_profiles WHERE ticker = ?", (test_ticker,)
        )

    # Fake Finnhub response — same shape as the real API
    fake_api_response = {
        "name": "Test Corporation",
        "marketCapitalization": 3_000_000,   # Finnhub reports in USD millions → $3T
        "finnhubIndustry": "Technology",
        "gsubind": "Software",
        "exchange": "NASDAQ",
        "country": "US",
    }

    # ── First call: expect a Finnhub API hit ──────────────────────────────
    stats1 = {"hits": 0, "misses": 0, "stale_fallback": 0, "skipped": 0}
    with patch("src.earnings._finnhub_get", return_value=fake_api_response) as mock_api:
        profile1 = _get_company_profile(test_ticker, stats1)

    assert mock_api.call_count == 1, (
        f"Expected 1 Finnhub call on cache miss, got {mock_api.call_count}"
    )
    assert profile1 is not None, "Profile should not be None on cache miss"
    assert profile1["market_cap_usd"] == 3_000_000_000_000, (
        f"Expected $3T market cap, got {profile1['market_cap_usd']}"
    )
    assert profile1["sector"] == "Technology"
    assert profile1["company_name"] == "Test Corporation"
    assert stats1["misses"] == 1
    assert stats1["hits"] == 0
    print(f"  ✅ First call: Finnhub API hit (miss), written to cache")

    # ── Second call: expect cache hit, zero API calls ─────────────────────
    stats2 = {"hits": 0, "misses": 0, "stale_fallback": 0, "skipped": 0}
    with patch("src.earnings._finnhub_get") as mock_api2:
        profile2 = _get_company_profile(test_ticker, stats2)

    assert mock_api2.call_count == 0, (
        f"Expected 0 Finnhub calls on cache hit, got {mock_api2.call_count}"
    )
    assert profile2 is not None, "Profile should not be None on cache hit"
    assert profile2["market_cap_usd"] == 3_000_000_000_000
    assert profile2["sector"] == "Technology"
    assert stats2["hits"] == 1
    assert stats2["misses"] == 0
    print(f"  ✅ Second call: cache hit, Finnhub not called")

    # ── Confirm the data actually lives in SQLite ─────────────────────────
    row = database.get_cached_profile(test_ticker)
    assert row is not None, "Row should exist in company_profiles table"
    assert row["company_name"] == "Test Corporation"
    assert row["sector"] == "Technology"
    assert row["last_updated"] is not None
    print(f"  ✅ SQLite row verified (last_updated: {row['last_updated']})")

    # Clean up test row
    with database.get_db() as conn:
        conn.execute(
            "DELETE FROM company_profiles WHERE ticker = ?", (test_ticker,)
        )

    print(f"  ✅ Profile cache test passed (TTL = {CACHE_TTL_HOURS}h)\n")


# ─────────────────────────────────────────────────────────────────────────
# 2. Env var check
# ─────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  Signal Pipeline — local test")
    print("=" * 60)

    # Cache test runs without API keys — always run it first
    test_profile_cache()

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

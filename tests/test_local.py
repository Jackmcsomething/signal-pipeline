"""
tests/test_local.py
───────────────────
Run this BEFORE deploying to confirm everything works.

Unit tests (no API keys required):
  1. test_profile_cache()        — SQLite profile cache: miss → API, hit → cache
  2. test_v2_apple_case()        — 3.1% beat, $4T cap, shares method → standard
  3. test_v2_roku_case()         — 71% beat, $15B cap, shares method → high
  4. test_v2_smallcap_noise()    — 6% beat, $2.5B cap, fallback, low surprise → below_threshold
  5. test_v2_disqualified()      — 1.5% beat → disqualified, no notify
  6. test_v2_miss_symmetry()     — -10% miss, $50B cap → conviction -5, tier high
  7. test_v2_fallback_method()   — no shares_outstanding → method = market_cap_fallback
  8. test_v2_reason_codes_format() — reason_codes list contains expected strings

Integration (requires API keys + live network):
  9.  check_env()               — all 4 env vars present
  10. send_test_notification()   — live Pushover ping to phone
  11. run_cycle()                — one full pipeline cycle
"""
import json
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

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from src import notify, database
from src.earnings import _get_company_profile, CACHE_TTL_HOURS
from src.scoring_v2 import score_earnings_v2
from run import run_cycle


# ─────────────────────────────────────────────────────────────────────────
# Shared test helpers
# ─────────────────────────────────────────────────────────────────────────

def _make_event(surprise_pct, eps_actual=None, eps_estimate=None):
    """Minimal earnings event dict for unit tests."""
    return {
        "source":       "earnings",
        "ticker":       "TEST",
        "surprise_pct": surprise_pct,
        "eps_actual":   eps_actual,
        "eps_estimate": eps_estimate,
    }

def _make_profile(market_cap_usd=None, shares_outstanding=None):
    """Minimal profile dict for unit tests."""
    return {
        "market_cap_usd":    market_cap_usd,
        "shares_outstanding": shares_outstanding,
    }


# ─────────────────────────────────────────────────────────────────────────
# 1. Profile cache
# ─────────────────────────────────────────────────────────────────────────

def test_profile_cache():
    """
    First call → cache miss → Finnhub API called → written to SQLite.
    Second call within 24h → cache hit → Finnhub NOT called.
    """
    print("\n▶ [1] Profile cache...")
    database.init_db()

    test_ticker = "_TEST_CACHE_TICKER_"
    with database.get_db() as conn:
        conn.execute("DELETE FROM company_profiles WHERE ticker = ?", (test_ticker,))

    fake_api_response = {
        "name":                   "Test Corporation",
        "marketCapitalization":   3_000_000,    # $3T (millions)
        "shareOutstanding":       15_000,        # 15B shares (millions)
        "finnhubIndustry":        "Technology",
        "gsubind":                "Software",
        "exchange":               "NASDAQ",
        "country":                "US",
    }

    # First call — expect 1 API hit
    stats1 = {"hits": 0, "misses": 0, "stale_fallback": 0, "skipped": 0}
    with patch("src.earnings._finnhub_get", return_value=fake_api_response) as mock_api:
        p1 = _get_company_profile(test_ticker, stats1)

    assert mock_api.call_count == 1, f"Expected 1 API call, got {mock_api.call_count}"
    assert p1["market_cap_usd"]    == 3_000_000_000_000
    assert p1["shares_outstanding"] == 15_000_000_000
    assert p1["sector"]            == "Technology"
    assert stats1["misses"] == 1 and stats1["hits"] == 0
    print("  ✅ First call: API hit, written to cache")

    # Second call — expect 0 API calls
    stats2 = {"hits": 0, "misses": 0, "stale_fallback": 0, "skipped": 0}
    with patch("src.earnings._finnhub_get") as mock_api2:
        p2 = _get_company_profile(test_ticker, stats2)

    assert mock_api2.call_count == 0, f"Expected 0 API calls, got {mock_api2.call_count}"
    assert p2["market_cap_usd"]    == 3_000_000_000_000
    assert p2["shares_outstanding"] == 15_000_000_000
    assert stats2["hits"] == 1 and stats2["misses"] == 0
    print("  ✅ Second call: cache hit, no API call")

    # Verify DB row
    row = database.get_cached_profile(test_ticker)
    assert row["company_name"]      == "Test Corporation"
    assert row["shares_outstanding"] == 15_000_000_000
    print(f"  ✅ SQLite row verified (last_updated: {row['last_updated']})")

    with database.get_db() as conn:
        conn.execute("DELETE FROM company_profiles WHERE ticker = ?", (test_ticker,))
    print(f"  ✅ Cache test passed (TTL = {CACHE_TTL_HOURS}h)\n")


# ─────────────────────────────────────────────────────────────────────────
# 2. Apple — standard conviction via shares method
# ─────────────────────────────────────────────────────────────────────────

def test_v2_apple_case():
    """
    3.1% beat, eps_actual=2.01, eps_estimate=1.95, shares=15B, mktcap=$4T
    abs_surprise = (2.01-1.95) * 15B = $900M → score 2  ($500M-$5B)
    magnitude=1 (2≤3.1<5), cap=1 ($4T≥$10B)
    abs_conviction = 1+2+1 = 4 → standard, would_notify=True
    """
    print("\n▶ [2] Apple case (standard)...")
    event   = _make_event(3.077, eps_actual=2.01, eps_estimate=1.95)
    profile = _make_profile(market_cap_usd=4_000_000_000_000,
                            shares_outstanding=15_000_000_000)

    r = score_earnings_v2(event, profile)

    assert r["magnitude_score"]           == 1,          f"magnitude: {r['magnitude_score']}"
    assert r["absolute_surprise_score"]   == 2,          f"surprise_score: {r['absolute_surprise_score']}"
    assert r["absolute_surprise_method"]  == "shares_outstanding"
    assert r["cap_modifier"]              == 1,          f"cap: {r['cap_modifier']}"
    assert r["conviction_score"]          == 4,          f"conviction: {r['conviction_score']}"
    assert r["tier"]                      == "standard", f"tier: {r['tier']}"
    assert r["would_notify"]              is True

    # Surprise USD: |2.01-1.95| * 15B = $900M
    assert 890_000_000 < r["absolute_surprise_usd"] < 910_000_000, (
        f"surprise_usd: {r['absolute_surprise_usd']}"
    )
    print(f"  ✅ conviction={r['conviction_score']}, tier={r['tier']}, "
          f"surprise=${r['absolute_surprise_usd']/1e6:.0f}M, "
          f"method={r['absolute_surprise_method']}")


# ─────────────────────────────────────────────────────────────────────────
# 3. Roku — high conviction
# ─────────────────────────────────────────────────────────────────────────

def test_v2_roku_case():
    """
    71% beat, eps_estimate=0.20, eps_actual=0.342, shares=500M, mktcap=$15B
    abs_surprise = |0.342-0.20| * 500M = $71M → score 1  ($50M-$500M)
    magnitude=4 (71%≥20), cap=1 ($15B≥$10B)
    abs_conviction = 4+1+1 = 6 → high, would_notify=True
    """
    print("\n▶ [3] Roku case (high)...")
    event   = _make_event(71.0, eps_actual=0.342, eps_estimate=0.20)
    profile = _make_profile(market_cap_usd=15_000_000_000,
                            shares_outstanding=500_000_000)

    r = score_earnings_v2(event, profile)

    assert r["magnitude_score"]          == 4,      f"magnitude: {r['magnitude_score']}"
    assert r["absolute_surprise_score"]  == 1,      f"surprise_score: {r['absolute_surprise_score']}"
    assert r["absolute_surprise_method"] == "shares_outstanding"
    assert r["cap_modifier"]             == 1,      f"cap: {r['cap_modifier']}"
    assert r["conviction_score"]         == 6,      f"conviction: {r['conviction_score']}"
    assert r["tier"]                     == "high", f"tier: {r['tier']}"
    assert r["would_notify"]             is True

    # abs_surprise: |0.342-0.20| * 500M = $71M
    assert 70_000_000 < r["absolute_surprise_usd"] < 72_000_000, (
        f"surprise_usd: {r['absolute_surprise_usd']}"
    )
    print(f"  ✅ conviction={r['conviction_score']}, tier={r['tier']}, "
          f"surprise=${r['absolute_surprise_usd']/1e6:.1f}M")


# ─────────────────────────────────────────────────────────────────────────
# 4. Small-cap noise — below threshold
# ─────────────────────────────────────────────────────────────────────────

def test_v2_smallcap_noise():
    """
    6% beat, mktcap=$2.5B, no shares_outstanding → market_cap_fallback
    fallback surprise = (6/100) * 2.5B * 0.10 = $15M → score 0 (< $50M)
    magnitude=2 (5≤6<10), cap=0 ($2.5B<$10B)
    abs_conviction = 2+0+0 = 2 → below_threshold, would_notify=False
    """
    print("\n▶ [4] Small-cap noise (below_threshold)...")
    event   = _make_event(6.0)
    profile = _make_profile(market_cap_usd=2_500_000_000)  # no shares_outstanding

    r = score_earnings_v2(event, profile)

    assert r["magnitude_score"]          == 2,               f"magnitude: {r['magnitude_score']}"
    assert r["absolute_surprise_score"]  == 0,               f"surprise_score: {r['absolute_surprise_score']}"
    assert r["absolute_surprise_method"] == "market_cap_fallback"
    assert r["cap_modifier"]             == 0,               f"cap: {r['cap_modifier']}"
    assert r["conviction_score"]         == 2,               f"conviction: {r['conviction_score']}"
    assert r["tier"]                     == "below_threshold", f"tier: {r['tier']}"
    assert r["would_notify"]             is False

    # fallback: (6/100) * 2.5B * 0.10 = $15M
    assert r["absolute_surprise_usd"] < 50_000_000, (
        f"surprise_usd should be < $50M, got {r['absolute_surprise_usd']}"
    )
    print(f"  ✅ conviction={r['conviction_score']}, tier={r['tier']}, "
          f"surprise=${r['absolute_surprise_usd']/1e6:.1f}M (fallback)")


# ─────────────────────────────────────────────────────────────────────────
# 5. Disqualified — beat% < 2%
# ─────────────────────────────────────────────────────────────────────────

def test_v2_disqualified():
    """
    1.5% beat → magnitude=0 → disqualified immediately, would_notify=False.
    No surprise or cap calculation performed.
    """
    print("\n▶ [5] Disqualified (< 2% beat)...")
    event   = _make_event(1.5)
    profile = _make_profile(market_cap_usd=50_000_000_000, shares_outstanding=1_000_000_000)

    r = score_earnings_v2(event, profile)

    assert r["magnitude_score"]   == 0,             f"magnitude: {r['magnitude_score']}"
    assert r["tier"]              == "disqualified", f"tier: {r['tier']}"
    assert r["would_notify"]      is False
    assert r["conviction_score"]  == 0
    print(f"  ✅ tier={r['tier']}, would_notify={r['would_notify']}")


# ─────────────────────────────────────────────────────────────────────────
# 6. Miss symmetry — sign preserved through tier mapping
# ─────────────────────────────────────────────────────────────────────────

def test_v2_miss_symmetry():
    """
    -10% miss, eps_estimate=2.00, eps_actual=1.80, shares=2B, mktcap=$50B
    abs_surprise = |1.80-2.00| * 2B = $400M → score 1  ($50M-$500M)
    magnitude=-3 (abs(10)=10, 10≤x<20, negative for miss), cap=1 ($50B≥$10B)
    abs_conviction = 3+1+1 = 5 → high, conviction_score = -5
    """
    print("\n▶ [6] Miss symmetry (high, negative)...")
    event   = _make_event(-10.0, eps_actual=1.80, eps_estimate=2.00)
    profile = _make_profile(market_cap_usd=50_000_000_000,
                            shares_outstanding=2_000_000_000)

    r = score_earnings_v2(event, profile)

    assert r["magnitude_score"]          == -3,     f"magnitude: {r['magnitude_score']}"
    assert r["absolute_surprise_score"]  == 1,      f"surprise_score: {r['absolute_surprise_score']}"
    assert r["cap_modifier"]             == 1,      f"cap: {r['cap_modifier']}"
    assert r["conviction_score"]         == -5,     f"conviction: {r['conviction_score']}"
    assert r["tier"]                     == "high", f"tier: {r['tier']}"
    assert r["would_notify"]             is True    # miss still notifies

    # Surprise: |1.80-2.00| * 2B = $400M
    assert 395_000_000 < r["absolute_surprise_usd"] < 405_000_000, (
        f"surprise_usd: {r['absolute_surprise_usd']}"
    )
    print(f"  ✅ conviction={r['conviction_score']}, tier={r['tier']}, "
          f"would_notify={r['would_notify']}")


# ─────────────────────────────────────────────────────────────────────────
# 7. Fallback method when shares_outstanding missing
# ─────────────────────────────────────────────────────────────────────────

def test_v2_fallback_method():
    """
    Profile without shares_outstanding → absolute_surprise_method = "market_cap_fallback".
    Surprise computed as (beat%/100) * market_cap * 0.10.
    """
    print("\n▶ [7] Fallback method (no shares_outstanding)...")

    # 15% beat, $100B mktcap, no shares
    # fallback surprise = (15/100) * 100B * 0.10 = $1.5B → score 2 ($500M-$5B)
    event   = _make_event(15.0, eps_actual=1.15, eps_estimate=1.00)
    profile = _make_profile(market_cap_usd=100_000_000_000)  # no shares_outstanding

    r = score_earnings_v2(event, profile)

    assert r["absolute_surprise_method"] == "market_cap_fallback", (
        f"Expected market_cap_fallback, got {r['absolute_surprise_method']}"
    )
    # (15/100) * 100B * 0.10 = $1.5B → surprise_score 2
    expected_usd = (15.0 / 100.0) * 100_000_000_000 * 0.10
    assert abs(r["absolute_surprise_usd"] - expected_usd) < 1, (
        f"surprise_usd {r['absolute_surprise_usd']} ≠ {expected_usd}"
    )
    assert r["absolute_surprise_score"] == 2, (
        f"$1.5B should be score 2, got {r['absolute_surprise_score']}"
    )
    print(f"  ✅ method={r['absolute_surprise_method']}, "
          f"surprise_usd=${r['absolute_surprise_usd']/1e9:.1f}B, "
          f"surprise_score={r['absolute_surprise_score']}")


# ─────────────────────────────────────────────────────────────────────────
# 8. Reason codes format
# ─────────────────────────────────────────────────────────────────────────

def test_v2_reason_codes_format():
    """
    Verify reason_codes is a list of strings with the expected components.
    Beat case:  ["magnitude:+X", "absolute_surprise:+X", "method:...", "cap_modifier:+X"]
    Miss case:  magnitude entry is negative: "magnitude:-X"
    Disqualified: ["magnitude:0", "disqualified"]
    """
    print("\n▶ [8] Reason codes format...")

    # ── Beat case ──
    event   = _make_event(12.0, eps_actual=1.12, eps_estimate=1.00)
    profile = _make_profile(market_cap_usd=20_000_000_000, shares_outstanding=500_000_000)
    r_beat  = score_earnings_v2(event, profile)

    codes = r_beat["reason_codes"]
    assert isinstance(codes, list),                   "reason_codes should be a list"
    assert any(c.startswith("magnitude:+")   for c in codes), f"No magnitude:+ in {codes}"
    assert any(c.startswith("absolute_surprise:+") for c in codes), f"No surprise in {codes}"
    assert any(c.startswith("method:")       for c in codes), f"No method in {codes}"
    assert any(c.startswith("cap_modifier:") for c in codes), f"No cap_modifier in {codes}"
    print(f"  ✅ Beat codes: {codes}")

    # ── Miss case: magnitude entry should be negative ──
    event_miss  = _make_event(-12.0, eps_actual=0.88, eps_estimate=1.00)
    r_miss      = score_earnings_v2(event_miss, profile)
    miss_codes  = r_miss["reason_codes"]
    assert any(c.startswith("magnitude:-") for c in miss_codes), (
        f"Expected magnitude:-X in miss codes: {miss_codes}"
    )
    print(f"  ✅ Miss codes: {miss_codes}")

    # ── Disqualified case ──
    r_disq = score_earnings_v2(_make_event(0.5), profile)
    assert "magnitude:0"   in r_disq["reason_codes"]
    assert "disqualified"  in r_disq["reason_codes"]
    print(f"  ✅ Disqualified codes: {r_disq['reason_codes']}")

    print("  ✅ Reason codes format test passed")


# ─────────────────────────────────────────────────────────────────────────
# 9–11. Integration tests (require real API keys)
# ─────────────────────────────────────────────────────────────────────────

def check_env():
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
    print("  Signal Pipeline — local test suite")
    print("=" * 60)

    # Unit tests — no API keys required
    test_profile_cache()
    test_v2_apple_case()
    test_v2_roku_case()
    test_v2_smallcap_noise()
    test_v2_disqualified()
    test_v2_miss_symmetry()
    test_v2_fallback_method()
    test_v2_reason_codes_format()

    print("\n" + "─" * 60)
    print("  All unit tests passed. Running integration checks...")
    print("─" * 60)

    if not check_env():
        sys.exit(1)

    print("\n▶ Sending test Pushover notification...")
    if notify.send_test_notification():
        print("✅ Test notification sent — check your phone")
    else:
        print("❌ Test notification failed — check Pushover keys")
        sys.exit(1)

    print("\n▶ Running one full pipeline cycle...")
    print("   (fetches from all three sources)\n")
    logging.getLogger().setLevel(logging.INFO)
    run_cycle()

    print("\n" + "=" * 60)
    print("  Done. Check signals.db to see what was logged.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

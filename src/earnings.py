"""
earnings.py
───────────
Detect earnings beats by polling Finnhub's earnings calendar API.

How Finnhub gives us this:
  GET /calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD
  Returns: list of upcoming/recent earnings with eps_actual, eps_estimate,
           revenue_actual, revenue_estimate, ticker, etc.

Strategy:
  - Poll the calendar for a window covering yesterday + today
  - For each company that has reported (eps_actual is not null), compute
    surprise % vs estimate
  - If surprise meets the notify threshold, persist the signal
  - The de-dup key is "EARNINGS_<ticker>_<period_end_date>" so we never
    double-count the same quarter

Company profile caching:
  Finnhub's /stock/profile2 (market cap + sector) is called at most once
  per ticker per 24 hours. Results are stored in the company_profiles SQLite
  table. On 429 errors we fall back to stale cached data if available, or
  skip the ticker for this cycle if not.

  Per-cycle summary is logged at the end of fetch_recent_earnings():
    Profile cache: X hits, Y misses, Z stale-fallback, W skipped

Finnhub API calls per cycle (earnings source only):
  Before caching:
    1 call for /calendar/earnings
    + N calls for /stock/profile2 (one per ticker that has reported)
    On a busy earnings day N can be 20-40, triggering 429s on the free tier.

  After caching (steady state, most tickers already cached):
    1 call for /calendar/earnings
    + 0-2 calls for /stock/profile2 (only new tickers or >24h-old entries)

  First cycle of the day (cold cache, all tickers stale/missing):
    1 call for /calendar/earnings
    + N calls for /stock/profile2 — same as before, but only happens once.
    Subsequent cycles that day: 1 call total.
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta

import requests

import config
from src import database

log = logging.getLogger(__name__)

# How long a cached profile is considered fresh before we re-fetch from Finnhub
CACHE_TTL_HOURS = 24


# ─────────────────────────────────────────────────────────────────────────
# Finnhub HTTP helper
# ─────────────────────────────────────────────────────────────────────────

def _finnhub_get(path: str, params: dict) -> dict:
    """
    Thin wrapper around Finnhub's REST API.

    Auth via X-Finnhub-Token header (not query param) so the key never
    appears in log messages, 429 error bodies, or server access logs.
    """
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY env var not set")

    url = f"{config.FINNHUB_BASE_URL}{path}"
    headers = {"X-Finnhub-Token": api_key}

    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()


# ─────────────────────────────────────────────────────────────────────────
# Surprise calculation
# ─────────────────────────────────────────────────────────────────────────

def _calculate_surprise_pct(actual: float, estimate: float) -> float | None:
    """
    Surprise % = (actual - estimate) / |estimate| * 100

    Using abs(estimate) so a negative estimate (loss expected) that comes
    in less negative still scores as a positive surprise.
    """
    if actual is None or estimate is None:
        return None
    if estimate == 0:
        # Avoid div-by-zero. If estimate is 0 and actual is positive,
        # that's effectively infinite surprise — cap it at +1000%.
        return 1000.0 if actual > 0 else (-1000.0 if actual < 0 else 0.0)
    return ((actual - estimate) / abs(estimate)) * 100


# ─────────────────────────────────────────────────────────────────────────
# Profile cache helpers
# ─────────────────────────────────────────────────────────────────────────

def _is_fresh(last_updated_iso: str) -> bool:
    """Return True if the cached profile is younger than CACHE_TTL_HOURS."""
    try:
        last_updated = datetime.fromisoformat(last_updated_iso)
        return (datetime.utcnow() - last_updated) < timedelta(hours=CACHE_TTL_HOURS)
    except (ValueError, TypeError):
        return False


def _cache_age(last_updated_iso: str) -> str:
    """Human-readable age string for log messages, e.g. '26.3h'."""
    try:
        delta = datetime.utcnow() - datetime.fromisoformat(last_updated_iso)
        return f"{delta.total_seconds() / 3600:.1f}h"
    except Exception:
        return "unknown"


def _profile_from_row(row: dict) -> dict:
    """
    Convert a company_profiles DB row to the standard profile dict shape
    used throughout earnings.py.
    """
    return {
        "company_name": row.get("company_name"),
        "market_cap_usd": row.get("market_cap_usd"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "exchange": row.get("exchange"),
        "country": row.get("country"),
    }


def _profile_from_api(data: dict) -> dict:
    """Parse a raw Finnhub /stock/profile2 response into the standard shape."""
    cap_millions = data.get("marketCapitalization")
    return {
        "company_name": data.get("name"),
        "market_cap_usd": cap_millions * 1_000_000 if cap_millions else None,
        "sector": data.get("finnhubIndustry"),
        "industry": data.get("gsubind"),
        "exchange": data.get("exchange"),
        "country": data.get("country"),
    }


# ─────────────────────────────────────────────────────────────────────────
# Main profile fetch with cache-first logic
# ─────────────────────────────────────────────────────────────────────────

def _get_company_profile(ticker: str, stats: dict) -> dict | None:
    """
    Return a profile dict for the given ticker.

    Cache-first behaviour:
      1. If a fresh (<24h) cache entry exists → return it (cache hit).
      2. If not → call Finnhub /stock/profile2:
           a. Success → write to cache, return fresh data (cache miss).
           b. 429 + stale cache exists → return stale data, log warning.
           c. 429 + no cache → skip this ticker this cycle (return None).
           d. Other error → same fallback logic as 429.

    `stats` is a mutable dict with keys hits/misses/stale_fallback/skipped.
    It is mutated in place so fetch_recent_earnings() can log a summary.

    Returns None only when we have no data at all — callers must skip the
    ticker for this cycle and retry on the next run.
    """
    cached = database.get_cached_profile(ticker)

    # ── Cache hit ──
    if cached and _is_fresh(cached["last_updated"]):
        stats["hits"] += 1
        return _profile_from_row(cached)

    # ── Cache miss or stale: try Finnhub ──
    try:
        data = _finnhub_get("/stock/profile2", {"symbol": ticker})
        profile = _profile_from_api(data)
        database.upsert_profile(ticker, profile)
        stats["misses"] += 1
        return profile

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        label = "429 rate-limited" if status == 429 else f"HTTP {status}"

        if cached:
            log.warning(
                f"{ticker}: Finnhub {label} — using stale profile "
                f"(age {_cache_age(cached['last_updated'])})"
            )
            stats["stale_fallback"] += 1
            return _profile_from_row(cached)
        else:
            log.warning(
                f"{ticker}: Finnhub {label} — no cache, skipping this cycle"
            )
            stats["skipped"] += 1
            return None

    except Exception as exc:
        if cached:
            log.warning(
                f"{ticker}: profile fetch error ({exc}) — using stale profile "
                f"(age {_cache_age(cached['last_updated'])})"
            )
            stats["stale_fallback"] += 1
            return _profile_from_row(cached)
        else:
            log.warning(
                f"{ticker}: profile fetch error ({exc}) — no cache, skipping"
            )
            stats["skipped"] += 1
            return None


def _is_uk_ticker(ticker: str) -> bool:
    """LSE tickers on Finnhub end in '.L' (e.g. 'LLOY.L')."""
    return ticker.endswith(".L")


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────

def fetch_recent_earnings() -> list[dict]:
    """
    Returns a list of earnings event dicts ready for scoring + persistence.

    Each dict has:
        event_id, source, ticker, company_name, market, market_cap_usd,
        event_time, surprise_pct, raw_data
    """
    # Window: yesterday → today. Catches after-hours US earnings + UK opens.
    today = datetime.utcnow().date()
    window_start = (today - timedelta(days=1)).isoformat()
    window_end = today.isoformat()

    log.info(f"Fetching Finnhub earnings calendar {window_start} → {window_end}")

    data = _finnhub_get(
        "/calendar/earnings",
        {"from": window_start, "to": window_end},
    )

    earnings_list = data.get("earningsCalendar", [])
    log.info(f"Finnhub returned {len(earnings_list)} earnings entries")

    # Per-cycle profile cache stats — logged as one line at the end
    stats = {"hits": 0, "misses": 0, "stale_fallback": 0, "skipped": 0}

    events = []
    for entry in earnings_list:
        ticker = entry.get("symbol")
        eps_actual = entry.get("epsActual")
        eps_estimate = entry.get("epsEstimate")
        rev_actual = entry.get("revenueActual")
        rev_estimate = entry.get("revenueEstimate")

        # Skip if hasn't reported yet
        if eps_actual is None or eps_estimate is None:
            continue

        # Skip if explicitly ignored
        if ticker in config.IGNORE_TICKERS:
            continue

        # Compute surprise
        eps_surprise = _calculate_surprise_pct(eps_actual, eps_estimate)
        rev_surprise = _calculate_surprise_pct(rev_actual, rev_estimate)

        # Apply revenue beat filter (avoids EPS beats driven only by buybacks)
        if (
            config.EARNINGS_MIN_REVENUE_BEAT > 0
            and rev_surprise is not None
            and rev_surprise < config.EARNINGS_MIN_REVENUE_BEAT
        ):
            log.debug(f"{ticker}: EPS beat but revenue weak ({rev_surprise:.1f}%), skipping")
            continue

        # Fetch company profile (market cap + sector), cache-first
        profile = _get_company_profile(ticker, stats)
        if profile is None:
            # 429 with no cached data — skip this ticker, retry next cycle
            continue

        market_cap = profile["market_cap_usd"]
        sector = profile["sector"]
        is_uk = _is_uk_ticker(ticker)

        # Sector filter — skip excluded sectors (e.g. Financials per dissertation)
        if sector and any(
            excluded.lower() in sector.lower()
            for excluded in config.IGNORE_SECTORS
        ):
            log.debug(f"{ticker}: sector '{sector}' is excluded, skipping")
            continue

        # Market cap filter
        # UK threshold in GBP, US in USD — for simplicity we treat them as
        # roughly equivalent (within 25%) since exact FX is overkill here.
        threshold = config.MIN_MARKET_CAP_GBP if is_uk else config.MIN_MARKET_CAP_USD
        if market_cap is not None and market_cap < threshold:
            log.debug(
                f"{ticker}: market cap ${market_cap / 1e6:.0f}m below threshold, skipping"
            )
            continue

        period_end = entry.get("date", today.isoformat())

        # Use the real company name from profile if available; fall back to symbol
        company_name = profile.get("company_name") or ticker

        events.append({
            "event_id": f"EARNINGS_{ticker}_{period_end}",
            "source": "earnings",
            "ticker": ticker,
            "company_name": company_name,
            "market": "UK" if is_uk else "US",
            "market_cap_usd": market_cap,
            "event_time": entry.get("date", datetime.utcnow().isoformat()),
            "surprise_pct": eps_surprise,
            "deal_size_usd": None,
            "deal_premium": None,
            "raw_data": json.dumps(entry),
        })

    log.info(
        f"Filtered to {len(events)} qualifying earnings events | "
        f"Profile cache: {stats['hits']} hits, {stats['misses']} misses, "
        f"{stats['stale_fallback']} stale-fallback, {stats['skipped']} skipped"
    )
    return events

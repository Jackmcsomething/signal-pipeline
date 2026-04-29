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
"""
import os
import json
import logging
from datetime import datetime, timedelta

import requests

import config

log = logging.getLogger(__name__)


def _finnhub_get(path: str, params: dict) -> dict:
    """Thin wrapper that adds the API key + handles errors."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY env var not set")

    params = {**params, "token": api_key}
    url = f"{config.FINNHUB_BASE_URL}{path}"

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


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


def _get_company_profile(ticker: str) -> dict:
    """
    Fetch company profile from Finnhub: market cap (USD), sector, industry.
    Returns dict with keys: market_cap_usd, sector, industry. Values may be None.
    Used to filter out small-caps and excluded sectors.
    """
    result = {"market_cap_usd": None, "sector": None, "industry": None}
    try:
        data = _finnhub_get("/stock/profile2", {"symbol": ticker})
        # Finnhub returns marketCapitalization in millions of USD
        cap_millions = data.get("marketCapitalization")
        if cap_millions:
            result["market_cap_usd"] = cap_millions * 1_000_000
        # finnhubIndustry is the closest thing Finnhub gives to a sector label
        result["sector"] = data.get("finnhubIndustry")
        result["industry"] = data.get("gsubind")  # GICS sub-industry, may be None
    except Exception as e:
        log.warning(f"Failed to get company profile for {ticker}: {e}")
    return result

def _is_uk_ticker(ticker: str) -> bool:
    """LSE tickers on Finnhub end in '.L' (e.g. 'LLOY.L')."""
    return ticker.endswith(".L")


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

        # Fetch company profile (market cap + sector) in one call
        profile = _get_company_profile(ticker)
        market_cap = profile["market_cap_usd"]
        sector = profile["sector"]
        is_uk = _is_uk_ticker(ticker)

        # Sector filter — skip excluded sectors (e.g. Financials per dissertation)
        if sector and any(excluded.lower() in sector.lower() for excluded in config.IGNORE_SECTORS):
            log.debug(f"{ticker}: sector '{sector}' is excluded, skipping")
            continue

        # Market cap filter
        # UK threshold in GBP, US in USD — for simplicity we treat them as
        # roughly equivalent (within 25%) since exact FX is overkill here.
        threshold = (
            config.MIN_MARKET_CAP_GBP if is_uk else config.MIN_MARKET_CAP_USD
        )
        if market_cap is not None and market_cap < threshold:
            log.debug(f"{ticker}: market cap ${market_cap/1e6:.0f}m below threshold, skipping")
            continue

        period_end = entry.get("date", today.isoformat())

        events.append({
            "event_id": f"EARNINGS_{ticker}_{period_end}",
            "source": "earnings",
            "ticker": ticker,
            "company_name": entry.get("symbol"),  # Finnhub doesn't always give name
            "market": "UK" if is_uk else "US",
            "market_cap_usd": market_cap,
            "event_time": entry.get("date", datetime.utcnow().isoformat()),
            "surprise_pct": eps_surprise,
            "deal_size_usd": None,
            "deal_premium": None,
            "raw_data": json.dumps(entry),
        })

    log.info(f"Filtered to {len(events)} qualifying earnings events")
    return events

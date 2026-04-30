"""
config.py
─────────
ALL tunable settings live here. Change values in this file and the rest of the
pipeline picks them up automatically. This is intentionally the ONLY place
you should need to edit for normal tuning.

Read top to bottom — sections are grouped by what they do.
"""

# ═══════════════════════════════════════════════════════════════════════════
# 1. EARNINGS FILTERS
# ═══════════════════════════════════════════════════════════════════════════
# An "earnings beat" = company's actual EPS exceeded analyst consensus estimate.
# Surprise % = (actual - estimate) / abs(estimate) * 100
#
# Tiered approach:
#   - Everything is logged to the database
#   - We only NOTIFY you if surprise >= EARNINGS_NOTIFY_THRESHOLD
#   - We mark as "HIGH CONVICTION" if surprise >= EARNINGS_HIGH_CONVICTION_THRESHOLD

EARNINGS_NOTIFY_THRESHOLD = 5.0        # % - notify if beat is at least this big
EARNINGS_HIGH_CONVICTION_THRESHOLD = 15.0  # % - high conviction (dissertation: bigger surprise = stronger drift)

# Also require a minimum revenue beat (sometimes EPS beats are achieved via
# buybacks/cost cuts and don't reflect real momentum). Set to 0 to disable.
EARNINGS_MIN_REVENUE_BEAT = 1.0  # %


# ═══════════════════════════════════════════════════════════════════════════
# 2. M&A FILTERS
# ═══════════════════════════════════════════════════════════════════════════
# We capture both UK (LSE RNS) and US (SEC EDGAR 8-K) deals.
# Only NOTIFY if the deal meets BOTH conditions below.

MA_MIN_DEAL_SIZE_USD = 100_000_000   # $100m minimum for US deals
MA_MIN_DEAL_SIZE_GBP = 100_000_000   # £100m minimum for UK deals

# Deal certainty:
#   - "firm"     = Rule 2.7 firm offer (UK) or definitive agreement (US 8-K Item 1.01/2.01)
#   - "possible" = Rule 2.4 possible offer / talks (more uncertain, often noise)
MA_REQUIRE_FIRM_OFFER = True


# ═══════════════════════════════════════════════════════════════════════════
# 3. STOCK UNIVERSE
# ═══════════════════════════════════════════════════════════════════════════
# Mid-cap and large-cap only — small-caps are illiquid and often unbuyable on
# Trading 212 anyway. Filter applied after fetching the event.

MIN_MARKET_CAP_USD = 2_000_000_000   # $2bn for US stocks (raised from $500m to filter illiquid noise)
MIN_MARKET_CAP_GBP = 2_000_000_000   # £2bn for UK stocks

# Tickers to always ignore (add anything you don't want alerts for)
# Example: penny stocks, OTC, anything not on Trading 212
IGNORE_TICKERS = {
    # add tickers here as strings, e.g. "GME", "AMC"
}

# Sectors to exclude based on dissertation findings.
# Financial sector showed 40% accuracy (worse than coin-flip) in the 70-event study,
# so we skip it. Finnhub's sector names vary slightly, so we match common variants.
IGNORE_SECTORS = {
    "Finance",
    "Financial Services",
    "Financials",
    "Banks",
    "Banking",
    "Insurance",
}


# ═══════════════════════════════════════════════════════════════════════════
# 4. SCORING (dissertation-style +1 / 0 / -1)
# ═══════════════════════════════════════════════════════════════════════════
# Each event gets scored. The score combines surprise magnitude, deal premium,
# and signal type. Used in the notification AND in the future performance
# tracker to validate whether bigger scores → bigger reactions.

SCORE_THRESHOLDS = {
    # earnings: surprise % → score
    "earnings_strong_beat": 10.0,    # >= 10% surprise → score +1
    "earnings_strong_miss": -10.0,   # <= -10% surprise → score -1
    # M&A: target premium % over last close → score
    "ma_high_premium": 30.0,         # >= 30% premium → score +1 strong
    "ma_low_premium": 10.0,          # < 10% premium → score 0 weak
}


# ═══════════════════════════════════════════════════════════════════════════
# 5. POLLING SCHEDULE
# ═══════════════════════════════════════════════════════════════════════════
# GitHub Actions has a 5-minute minimum cron interval — that's our floor.
# These hours are in UTC. Defaults cover both UK (08:00-16:30 GMT/BST) and
# US (14:30-21:00 UTC during DST, 13:30-21:00 outside DST) trading hours,
# plus some pre/post-market buffer for after-hours earnings releases.

POLL_INTERVAL_MINUTES = 5

# Hours of day (UTC) during which to actually run. The Actions schedule
# fires every 5 mins 24/7, but the script no-ops outside these hours to
# save API quota.
ACTIVE_HOURS_UTC_START = 7   # 07:00 UTC = 08:00 BST (LSE pre-open)
ACTIVE_HOURS_UTC_END = 22    # 22:00 UTC = catches US after-market earnings


# ═══════════════════════════════════════════════════════════════════════════
# 6. NOTIFICATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════════
# Pushover priority: -2 (silent) | -1 (quiet) | 0 (normal) | 1 (high) | 2 (emergency)

PUSHOVER_PRIORITY_NORMAL = 0
PUSHOVER_PRIORITY_HIGH_CONVICTION = 1   # bypasses your phone's quiet hours

# Sound for the notification (see pushover.net/api#sounds for options)
PUSHOVER_SOUND_NORMAL = "pushover"
PUSHOVER_SOUND_HIGH_CONVICTION = "cashregister"

# Trading 212 deep link template — tap the notification → opens 212 to that ticker
# Format: https://www.trading212.com/equity/<TICKER>
TRADING_212_URL_TEMPLATE = "https://www.trading212.com/equity/{ticker}"


# ═══════════════════════════════════════════════════════════════════════════
# 7. AI TAKE (Claude one-liner)
# ═══════════════════════════════════════════════════════════════════════════
# We send each event to Claude with this prompt to generate a 1-line take.
# Keep it short — long takes get truncated in iOS notifications.

AI_TAKE_MODEL = "claude-haiku-4-5-20251001"  # fastest + cheapest, fine for this
AI_TAKE_MAX_TOKENS = 100
AI_TAKE_PROMPT_TEMPLATE = """You are analyzing a market event for a trader who acts on the 1-3 day post-announcement window.

Event: {event_summary}

In ONE sentence (max 25 words), tell the trader whether this fits the classic post-announcement drift pattern (strong surprise + clear catalyst + liquid name = high conviction). Be direct. No hedging."""


# ═══════════════════════════════════════════════════════════════════════════
# 8. DATABASE
# ═══════════════════════════════════════════════════════════════════════════
DATABASE_PATH = "signals.db"  # SQLite file, lives in repo root


# ═══════════════════════════════════════════════════════════════════════════
# 9. API ENDPOINTS (rarely need to change)
# ═══════════════════════════════════════════════════════════════════════════
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
SEC_EDGAR_BASE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
LSE_RNS_BASE_URL = "https://www.londonstockexchange.com/news"

# SEC requires a User-Agent header identifying you. Change to your details.
SEC_USER_AGENT = "Signal Pipeline jackgwhite@yahoo.co.uk"

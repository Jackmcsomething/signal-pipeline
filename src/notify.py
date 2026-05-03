"""
notify.py
─────────
Send Pushover notifications to your phone.

Pushover API:
  POST https://api.pushover.net/1/messages.json
  Required params: token (your app token), user (your user key), message
  Optional: title, priority, sound, url, url_title

Notification format by source:

  EARNINGS (v2 format):
    Title:   📈 BEAT +3.1% │ AAPL
    Body:    Apple Inc.
             Conviction: 4/8 STANDARD
             Surprise: ~$900M | Mega-cap
             Reasons: magnitude+1, surprise+2, cap+1
             Long watchlist signal
             💭 <AI take>

    Misses use 📉, "MISS", and append "Avoid / negative signal" instead.
    "(estimated)" suffix on surprise figure when method = market_cap_fallback.
    Priority mapped to v2 tier: standard=0, high=1, very_high=2 (emergency).

  M&A (v1 format, unchanged):
    Title:   🤝 M&A US │ $1.2B │ TICKER
    Body:    Company Name
             Score: +1 STRONG
             💭 <AI take>

iOS notification length limits:
  - Lock screen preview: ~2 lines, ~80 chars
  - Expanded notification: full message visible
  We aim for: title + first 80 chars of body tells the story.
"""
from __future__ import annotations

import json
import logging
import os

import requests

import config

log = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Maximum possible v2 conviction score: magnitude(4) + surprise(3) + cap(1) = 8
V2_MAX_CONVICTION = 8

# Tier → Pushover priority
V2_TIER_PRIORITY = {
    "standard":        0,
    "high":            1,
    "very_high":       2,   # emergency — also needs retry + expire params
    # below_threshold and disqualified should not reach send_pushover,
    # but default to 0 if they somehow do
    "below_threshold": 0,
    "disqualified":    0,
}

V2_TIER_SOUND = {
    "standard":        config.PUSHOVER_SOUND_NORMAL,
    "high":            config.PUSHOVER_SOUND_HIGH_CONVICTION,
    "very_high":       config.PUSHOVER_SOUND_HIGH_CONVICTION,
    "below_threshold": config.PUSHOVER_SOUND_NORMAL,
    "disqualified":    config.PUSHOVER_SOUND_NORMAL,
}

# Short display names for reason code keys
_REASON_KEY_DISPLAY = {
    "magnitude":         "magnitude",
    "absolute_surprise": "surprise",
    "cap_modifier":      "cap",
}


# ─────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────

def _cap_tier_label(market_cap_usd: float | None) -> str:
    """Human-readable cap tier for the notification Surprise line."""
    if market_cap_usd is None:
        return "Cap unknown"
    if market_cap_usd >= 100_000_000_000:
        return "Mega-cap"
    if market_cap_usd >= 10_000_000_000:
        return "Large-cap"
    return "Mid-cap"


def _format_surprise_usd(value: float, method: str) -> str:
    """Format absolute surprise as a short string with (estimated) suffix if needed."""
    if value >= 1_000_000_000:
        s = f"~${value / 1e9:.1f}B"
    elif value >= 1_000_000:
        s = f"~${value / 1e6:.0f}M"
    else:
        s = f"~${value:,.0f}"
    if method == "market_cap_fallback":
        s += " (estimated)"
    return s


def _format_reason_codes(reason_codes: list[str]) -> str:
    """
    Convert reason_codes list to a compact display string.
    Skips the "method:..." entry (shown separately on the Surprise line).
    ["magnitude:+1", "absolute_surprise:+2", "method:shares_outstanding", "cap_modifier:+1"]
      → "magnitude+1, surprise+2, cap+1"
    """
    parts = []
    for code in reason_codes:
        if code.startswith("method:") or code == "disqualified":
            continue
        key, _, val = code.partition(":")
        display = _REASON_KEY_DISPLAY.get(key, key)
        parts.append(f"{display}{val}")
    return ", ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Title builders
# ─────────────────────────────────────────────────────────────────────────

def _format_title(event: dict) -> str:
    """
    Build the notification title.
    Earnings:  📈 BEAT +3.1% │ AAPL
    M&A US:    🤝 M&A US │ $1.2B │ TICKER
    M&A UK:    🤝 M&A UK │ ~£500M │ TICKER
    """
    source = event["source"]
    ticker = event.get("ticker") or event.get("company_name", "?")[:20]

    if source == "earnings":
        surprise = event.get("surprise_pct", 0)
        emoji    = "📈" if surprise >= 0 else "📉"
        label    = "BEAT" if surprise >= 0 else "MISS"
        return f"{emoji} {label} {surprise:+.1f}% │ {ticker}"

    if source == "ma_us":
        size = event.get("deal_size_usd")
        size_str = (
            f"${size / 1e9:.1f}B" if size and size >= 1e9
            else (f"${size / 1e6:.0f}M" if size else "?")
        )
        return f"🤝 M&A US │ {size_str} │ {ticker}"

    if source == "ma_uk":
        size = event.get("deal_size_usd")
        size_str = f"~£{size / 1.27 / 1e6:.0f}M" if size else "?"
        return f"🤝 M&A UK │ {size_str} │ {ticker}"

    return f"⚡ Signal │ {ticker}"


# ─────────────────────────────────────────────────────────────────────────
# Body builders
# ─────────────────────────────────────────────────────────────────────────

def _format_earnings_message(event: dict, ai_take: str) -> str:
    """
    v2 earnings notification body.

    Apple Inc.
    Conviction: 4/8 STANDARD
    Surprise: ~$900M | Mega-cap
    Reasons: magnitude+1, surprise+2, cap+1
    Long watchlist signal

    💭 <AI take>
    """
    company        = event.get("company_name", "")
    conviction     = event.get("v2_conviction_score", 0)
    tier           = event.get("v2_tier", "").upper().replace("_", " ")
    surprise_usd   = event.get("v2_absolute_surprise_usd", 0.0) or 0.0
    method         = event.get("v2_absolute_surprise_method", "market_cap_fallback")
    market_cap     = event.get("market_cap_usd")

    # Parse reason_codes — stored as JSON string in DB, may be a list in memory
    raw_codes = event.get("v2_reason_codes", "[]")
    if isinstance(raw_codes, str):
        try:
            reason_list = json.loads(raw_codes)
        except (json.JSONDecodeError, TypeError):
            reason_list = []
    else:
        reason_list = raw_codes

    conviction_str  = f"{conviction}/{V2_MAX_CONVICTION}"
    surprise_str    = _format_surprise_usd(surprise_usd, method)
    cap_label       = _cap_tier_label(market_cap)
    reasons_str     = _format_reason_codes(reason_list)
    action_line     = (
        "Long watchlist signal" if conviction >= 0
        else "Avoid / negative signal"
    )

    return (
        f"{company}\n"
        f"Conviction: {conviction_str} {tier}\n"
        f"Surprise: {surprise_str} | {cap_label}\n"
        f"Reasons: {reasons_str}\n"
        f"{action_line}\n"
        f"\n"
        f"💭 {ai_take}"
    )


def _format_ma_message(event: dict, ai_take: str) -> str:
    """v1 M&A notification body — unchanged from original."""
    company    = event.get("company_name", "")
    score      = event.get("score", 0)
    score_str  = "+1 STRONG" if score == 1 else ("-1 STRONG" if score == -1 else "0 NEUTRAL")
    hc_str     = " · 🔥 HIGH CONVICTION" if event.get("is_high_conviction") else ""

    return (
        f"{company}\n"
        f"\n"
        f"Score: {score_str}{hc_str}\n"
        f"\n"
        f"💭 {ai_take}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────

def send_pushover(event: dict, ai_take: str) -> bool:
    """
    Send a Pushover notification. Returns True on success, False on failure.

    Priority and message format differ by source:
      earnings → v2 tier determines priority; v2 format body
      M&A      → v1 score determines priority; v1 format body
    """
    user_key  = os.environ.get("PUSHOVER_USER_KEY")
    app_token = os.environ.get("PUSHOVER_APP_TOKEN")

    if not user_key or not app_token:
        log.error("Pushover credentials not set in env vars")
        return False

    title = _format_title(event)

    source = event.get("source", "")

    if source == "earnings":
        message  = _format_earnings_message(event, ai_take)
        tier     = event.get("v2_tier", "standard")
        priority = V2_TIER_PRIORITY.get(tier, 0)
        sound    = V2_TIER_SOUND.get(tier, config.PUSHOVER_SOUND_NORMAL)
    else:
        message  = _format_ma_message(event, ai_take)
        is_hc    = event.get("is_high_conviction", 0)
        priority = (
            config.PUSHOVER_PRIORITY_HIGH_CONVICTION if is_hc
            else config.PUSHOVER_PRIORITY_NORMAL
        )
        sound = (
            config.PUSHOVER_SOUND_HIGH_CONVICTION if is_hc
            else config.PUSHOVER_SOUND_NORMAL
        )

    # Build the deep link to Trading 212
    ticker = event.get("ticker", "").replace(".L", "")
    payload: dict = {
        "token":   app_token,
        "user":    user_key,
        "title":   title,
        "message": message,
        "priority": priority,
        "sound":   sound,
    }
    if ticker:
        payload["url"]       = config.TRADING_212_URL_TEMPLATE.format(ticker=ticker)
        payload["url_title"] = f"Open {ticker} in Trading 212"

    # Pushover priority 2 (emergency) requires retry + expire params
    if priority == 2:
        payload["retry"]  = 60    # retry every 60s
        payload["expire"] = 600   # stop retrying after 10 minutes

    try:
        response = requests.post(PUSHOVER_API, data=payload, timeout=10)
        response.raise_for_status()
        log.info(f"Pushover sent: {title}")
        return True
    except Exception as exc:
        log.error(f"Pushover send failed: {exc}")
        return False


def send_test_notification() -> bool:
    """For local testing — sends a 'pipeline is working' ping using a v2-style event."""
    fake_event = {
        "source":                      "earnings",
        "ticker":                      "TEST",
        "company_name":                "Test Corp",
        "surprise_pct":                12.5,
        "market_cap_usd":              50_000_000_000,
        # v1 fields
        "score":                       1,
        "is_high_conviction":          1,
        "v1_score":                    1,
        "v1_high_conviction":          1,
        "v1_would_notify":             1,
        # v2 fields
        "v2_magnitude_score":          3,
        "v2_absolute_surprise_score":  1,
        "v2_absolute_surprise_usd":    75_000_000,
        "v2_absolute_surprise_method": "market_cap_fallback",
        "v2_cap_modifier":             1,
        "v2_conviction_score":         5,
        "v2_tier":                     "high",
        "v2_would_notify":             1,
        "v2_reason_codes":             json.dumps([
            "magnitude:+3", "absolute_surprise:+1",
            "method:market_cap_fallback", "cap_modifier:+1",
        ]),
    }
    return send_pushover(
        fake_event,
        "Test notification — Signal Pipeline v2 is wired up correctly.",
    )

"""
notify.py
─────────
Send Pushover notifications to your phone.

Pushover API:
  POST https://api.pushover.net/1/messages.json
  Required params: token (your app token), user (your user key), message
  Optional: title, priority, sound, url, url_title

iOS notification length limits:
  - Lock screen preview: ~2 lines, ~80 chars
  - Expanded notification: full message visible
  We aim for: title (concise) + first 80 chars of message tells the story
"""
import os
import logging
import requests

import config

log = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def _format_title(event: dict) -> str:
    """
    Build the notification title — appears bold at the top.
    Format: "📈 BEAT 12.3% │ AAPL"  or  "🤝 M&A FIRM │ £1.2B"
    """
    source = event["source"]
    ticker = event.get("ticker") or event.get("company_name", "?")[:20]

    if source == "earnings":
        surprise = event.get("surprise_pct", 0)
        emoji = "📈" if surprise > 0 else "📉"
        label = "BEAT" if surprise > 0 else "MISS"
        return f"{emoji} {label} {surprise:+.1f}% │ {ticker}"

    if source == "ma_us":
        size = event.get("deal_size_usd")
        size_str = f"${size/1e9:.1f}B" if size and size >= 1e9 else (
            f"${size/1e6:.0f}M" if size else "?"
        )
        return f"🤝 M&A US │ {size_str} │ {ticker}"

    if source == "ma_uk":
        size = event.get("deal_size_usd")
        size_str = f"~£{size/1.27/1e6:.0f}M" if size else "?"
        return f"🤝 M&A UK │ {size_str} │ {ticker}"

    return f"⚡ Signal │ {ticker}"


def _format_message(event: dict, ai_take: str) -> str:
    """Build the notification body."""
    company = event.get("company_name", "")
    score = event.get("score", 0)
    score_str = "+1 STRONG" if score == 1 else ("-1 STRONG" if score == -1 else "0 NEUTRAL")
    hc_str = " · 🔥 HIGH CONVICTION" if event.get("is_high_conviction") else ""

    return (
        f"{company}\n"
        f"\n"
        f"Score: {score_str}{hc_str}\n"
        f"\n"
        f"💭 {ai_take}"
    )


def send_pushover(event: dict, ai_take: str) -> bool:
    """
    Send a Pushover notification. Returns True on success, False on failure.
    """
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    app_token = os.environ.get("PUSHOVER_APP_TOKEN")

    if not user_key or not app_token:
        log.error("Pushover credentials not set in env vars")
        return False

    title = _format_title(event)
    message = _format_message(event, ai_take)

    # High-conviction events get higher priority + different sound
    is_hc = event.get("is_high_conviction", 0)
    priority = (
        config.PUSHOVER_PRIORITY_HIGH_CONVICTION if is_hc
        else config.PUSHOVER_PRIORITY_NORMAL
    )
    sound = (
        config.PUSHOVER_SOUND_HIGH_CONVICTION if is_hc
        else config.PUSHOVER_SOUND_NORMAL
    )

    # Build the deep link to Trading 212 (only useful if we have a ticker)
    ticker = event.get("ticker", "").replace(".L", "")  # 212 doesn't use the .L suffix
    payload = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
        "sound": sound,
    }
    if ticker:
        payload["url"] = config.TRADING_212_URL_TEMPLATE.format(ticker=ticker)
        payload["url_title"] = f"Open {ticker} in Trading 212"

    try:
        response = requests.post(PUSHOVER_API, data=payload, timeout=10)
        response.raise_for_status()
        log.info(f"Pushover sent: {title}")
        return True
    except Exception as e:
        log.error(f"Pushover send failed: {e}")
        return False


def send_test_notification() -> bool:
    """For local testing — sends a 'pipeline is working' ping."""
    fake_event = {
        "source": "earnings",
        "ticker": "TEST",
        "company_name": "Test Corp",
        "surprise_pct": 12.5,
        "score": 1,
        "is_high_conviction": 1,
    }
    return send_pushover(
        fake_event,
        "This is a test notification confirming your Signal Pipeline is wired up correctly.",
    )

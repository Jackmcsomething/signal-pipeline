"""
scoring.py
──────────
Score each event using a +1 / 0 / -1 system inspired by Jack's dissertation
methodology (news sentiment scoring across 63 events / 7 sectors).

The score does two things:
  1. Decides whether to NOTIFY (score != 0 → notify)
  2. Gets logged so we can later regress: score → 1/2/3 day return.
     If higher absolute scores correlate with bigger reactions, we've
     replicated the dissertation finding.

Scoring rules:
  EARNINGS:
    surprise >= +10%  → +1   (strong beat, bullish drift expected)
    surprise <= -10%  → -1   (strong miss, bearish drift expected)
    -10% < surprise < +10% → 0  (noise — but still notify if >5% per config)

  M&A:
    Firm offer with deal premium >= 30%  → +1  (strong bullish for target)
    Firm offer with deal premium <  30%  →  0  (target moves but smaller pop)
    "No intention to bid" / withdrawal   → -1  (bearish — target drops back)

  HIGH CONVICTION FLAG (separate from score):
    Set when surprise >= 10% (earnings) or deal premium >= 40% (M&A).
    Triggers a higher-priority Pushover notification.
"""
import logging
import config

log = logging.getLogger(__name__)


def score_earnings(event: dict) -> tuple[int, bool]:
    """Returns (score, is_high_conviction)."""
    surprise = event.get("surprise_pct")
    if surprise is None:
        return 0, False

    score = 0
    if surprise >= config.SCORE_THRESHOLDS["earnings_strong_beat"]:
        score = 1
    elif surprise <= config.SCORE_THRESHOLDS["earnings_strong_miss"]:
        score = -1

    is_high_conviction = (
        surprise >= config.EARNINGS_HIGH_CONVICTION_THRESHOLD
    )

    return score, is_high_conviction


def score_ma(event: dict) -> tuple[int, bool]:
    """Returns (score, is_high_conviction)."""
    premium = event.get("deal_premium")
    deal_size = event.get("deal_size_usd")

    # If we have no premium info, treat firm offers as +1 (bullish for target)
    # since by definition they're being acquired at some premium.
    if premium is None:
        # Firm offer with no premium info but deal size is meaningful
        if deal_size and deal_size >= config.MA_MIN_DEAL_SIZE_USD:
            return 1, False
        return 0, False

    score = 0
    if premium >= config.SCORE_THRESHOLDS["ma_high_premium"]:
        score = 1
    elif premium >= config.SCORE_THRESHOLDS["ma_low_premium"]:
        score = 1  # still bullish, just lower conviction
    else:
        score = 0

    is_high_conviction = premium >= 40.0

    return score, is_high_conviction


def score_event(event: dict) -> dict:
    """
    Mutates and returns the event dict with score + is_high_conviction set.
    Dispatches based on event source.
    """
    source = event["source"]

    if source == "earnings":
        score, hc = score_earnings(event)
    elif source in ("ma_us", "ma_uk"):
        score, hc = score_ma(event)
    else:
        log.warning(f"Unknown source for scoring: {source}")
        score, hc = 0, False

    event["score"] = score
    event["is_high_conviction"] = 1 if hc else 0  # SQLite uses int for bool

    log.debug(
        f"Scored {event.get('ticker') or event.get('company_name')}: "
        f"score={score}, high_conviction={hc}"
    )

    return event

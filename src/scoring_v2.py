"""
scoring_v2.py
─────────────
Multi-factor earnings conviction scoring. Runs in parallel with v1 during
the 2-week paper test so both systems can be compared on the same live data.

Why v2:
  v1 scores earnings as +1 / 0 / -1 purely on percentage surprise. A 12%
  beat at a $500M micro-cap and a 12% beat at Apple both score +1, even
  though the Apple beat represents billions of dollars of earnings revision
  that institutional desks will need to reposition around. v2 adds absolute
  dollar surprise and market cap as separate dimensions.

Scoring dimensions (all on absolute values; sign re-applied at the end):

  Magnitude (0–4):
    abs(beat%) < 2   → 0  (disqualified — noise level, do not notify)
    2  ≤ beat% < 5   → 1
    5  ≤ beat% < 10  → 2
    10 ≤ beat% < 20  → 3
    beat% ≥ 20       → 4

  Absolute surprise (0–3):
    Primary:  abs(eps_actual – eps_estimate) × shares_outstanding  [shares method]
    Fallback: (abs(beat%) / 100) × market_cap_usd × 0.10          [mkt-cap proxy]
              (0.10 = rough earnings margin proxy, P/E ≈ 10)
    < $50M    → 0
    $50M–$500M  → 1
    $500M–$5B   → 2
    ≥ $5B     → 3

  Cap modifier (0 or +1):
    market_cap_usd < $10B  → 0
    market_cap_usd ≥ $10B  → +1  (large / mega-cap names have more repo
                                   flow that drives post-announcement drift)

  Conviction:
    abs_conviction  = abs(magnitude_score) + absolute_surprise_score + cap_modifier
    conviction_score = abs_conviction if beat, –abs_conviction if miss
    Maximum possible: 4 + 3 + 1 = 8

  Tier (on abs(conviction_score)):
    0       → "disqualified"    (magnitude = 0; beat% < 2%)
    1–2     → "below_threshold" (logged but no notification)
    3–4     → "standard"
    5–6     → "high"
    7–8     → "very_high"

Metadata:
  absolute_surprise_method: "shares_outstanding" | "market_cap_fallback"
  reason_codes: list of strings documenting each component, e.g.:
    ["magnitude:+2", "absolute_surprise:+1", "method:shares_outstanding", "cap_modifier:+1"]
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Thresholds (not in config.py — these are v2 structural constants,
# not user-tunable filters)
# ─────────────────────────────────────────────────────────────────────────

MAGNITUDE_BANDS = [
    (20.0, 4),
    (10.0, 3),
    (5.0,  2),
    (2.0,  1),
    (0.0,  0),   # disqualified
]

SURPRISE_BANDS = [
    (5_000_000_000,   3),
    (500_000_000,     2),
    (50_000_000,      1),
    (0,               0),
]

CAP_MODIFIER_THRESHOLD = 10_000_000_000   # $10B

TIER_MAP = {
    0: ("disqualified",    False),
    1: ("below_threshold", False),
    2: ("below_threshold", False),
    3: ("standard",        True),
    4: ("standard",        True),
    5: ("high",            True),
    6: ("high",            True),
    7: ("very_high",       True),
    8: ("very_high",       True),
}


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────

def _magnitude_score(abs_beat_pct: float) -> int:
    for threshold, score in MAGNITUDE_BANDS:
        if abs_beat_pct >= threshold:
            return score
    return 0


def _surprise_score(abs_surprise_usd: float) -> int:
    for threshold, score in SURPRISE_BANDS:
        if abs_surprise_usd >= threshold:
            return score
    return 0


def _cap_modifier(market_cap_usd: float | None) -> int:
    if market_cap_usd is None:
        return 0
    return 1 if market_cap_usd >= CAP_MODIFIER_THRESHOLD else 0


def _tier(abs_conviction: int) -> tuple[str, bool]:
    """Return (tier_name, would_notify). Clamps to max of 8."""
    return TIER_MAP.get(min(abs_conviction, 8), ("very_high", True))


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────

def score_earnings_v2(event: dict, profile: dict) -> dict:
    """
    Score a single earnings event using the v2 multi-factor model.

    Parameters
    ----------
    event   : earnings event dict (must contain surprise_pct, epsActual,
              epsEstimate from raw_data — or eps_actual / eps_estimate
              as top-level keys if present)
    profile : company profile dict (from cache or Finnhub), must contain
              market_cap_usd; optionally shares_outstanding

    Returns
    -------
    dict with keys: magnitude_score, absolute_surprise_score,
    absolute_surprise_usd, absolute_surprise_method, cap_modifier,
    conviction_score, tier, would_notify, reason_codes
    """
    import json

    surprise_pct = event.get("surprise_pct") or 0.0
    abs_beat_pct = abs(surprise_pct)
    is_beat = surprise_pct >= 0

    market_cap = profile.get("market_cap_usd") if profile else None
    shares_out  = profile.get("shares_outstanding") if profile else None

    # ── Magnitude ──────────────────────────────────────────────────────────
    mag = _magnitude_score(abs_beat_pct)

    # Disqualify immediately if magnitude = 0 (< 2% beat — noise level)
    if mag == 0:
        return {
            "magnitude_score": 0,
            "absolute_surprise_score": 0,
            "absolute_surprise_usd": 0.0,
            "absolute_surprise_method": "n/a",
            "cap_modifier": 0,
            "conviction_score": 0,
            "tier": "disqualified",
            "would_notify": False,
            "reason_codes": ["magnitude:0", "disqualified"],
        }

    # ── Absolute surprise ──────────────────────────────────────────────────
    # Prefer shares_outstanding method; fall back to market cap proxy.
    # eps_actual / eps_estimate may be top-level or inside raw_data JSON.
    eps_actual   = event.get("eps_actual")
    eps_estimate = event.get("eps_estimate")

    # If not top-level, try to pull from raw_data JSON blob
    if (eps_actual is None or eps_estimate is None) and event.get("raw_data"):
        try:
            raw = json.loads(event["raw_data"])
            eps_actual   = eps_actual   if eps_actual   is not None else raw.get("epsActual")
            eps_estimate = eps_estimate if eps_estimate is not None else raw.get("epsEstimate")
        except (json.JSONDecodeError, TypeError):
            pass

    abs_surprise_usd: float
    method: str

    if shares_out is not None and eps_actual is not None and eps_estimate is not None:
        abs_surprise_usd = abs(eps_actual - eps_estimate) * shares_out
        method = "shares_outstanding"
    elif market_cap is not None:
        abs_surprise_usd = (abs_beat_pct / 100.0) * market_cap * 0.10
        method = "market_cap_fallback"
    else:
        # No data at all — treat surprise as 0
        abs_surprise_usd = 0.0
        method = "market_cap_fallback"

    surp_score = _surprise_score(abs_surprise_usd)

    # ── Cap modifier ───────────────────────────────────────────────────────
    cap_mod = _cap_modifier(market_cap)

    # ── Conviction ─────────────────────────────────────────────────────────
    abs_conviction = mag + surp_score + cap_mod
    conviction_score = abs_conviction if is_beat else -abs_conviction

    # ── Tier ───────────────────────────────────────────────────────────────
    tier_name, would_notify = _tier(abs_conviction)

    # ── Reason codes ───────────────────────────────────────────────────────
    sign = "+" if is_beat else "-"
    reason_codes = [
        f"magnitude:{sign}{mag}",
        f"absolute_surprise:+{surp_score}",
        f"method:{method}",
        f"cap_modifier:+{cap_mod}",
    ]

    return {
        "magnitude_score":            mag if is_beat else -mag,
        "absolute_surprise_score":    surp_score,
        "absolute_surprise_usd":      abs_surprise_usd,
        "absolute_surprise_method":   method,
        "cap_modifier":               cap_mod,
        "conviction_score":           conviction_score,
        "tier":                       tier_name,
        "would_notify":               would_notify,
        "reason_codes":               reason_codes,
    }

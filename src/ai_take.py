"""
ai_take.py
──────────
Generate a 1-line take on each event using Claude.

Why an LLM here:
  Raw numbers don't always tell the story. A 15% earnings beat at a tiny,
  illiquid name might not trade well; a 6% beat at a megacap with a strong
  guide raise might be a screamer. The LLM can apply qualitative judgement
  in a way pure rules can't.

Cost: each call uses ~150 input tokens + ~50 output tokens with Haiku.
At thousands of events/month that's still under £1.

Failure mode: if the API call fails (rate limit, network, etc), we fall
back to a templated string so the notification still goes out.
"""
import os
import logging
import anthropic

import config

log = logging.getLogger(__name__)


def _build_event_summary(event: dict) -> str:
    """Compact one-paragraph summary we feed Claude."""
    source = event["source"]
    ticker = event.get("ticker") or "(no ticker)"
    company = event.get("company_name") or ticker
    market = event.get("market", "?")

    if source == "earnings":
        surprise = event.get("surprise_pct", 0)
        market_cap = event.get("market_cap_usd")
        cap_str = f"${market_cap/1e9:.1f}B mkt cap" if market_cap else "size unknown"
        return (
            f"{company} ({ticker}, {market}) reported earnings with "
            f"{surprise:+.1f}% EPS surprise vs consensus. {cap_str}."
        )

    if source in ("ma_us", "ma_uk"):
        deal_size = event.get("deal_size_usd")
        size_str = f"${deal_size/1e9:.2f}B deal" if deal_size else "size undisclosed"
        premium = event.get("deal_premium")
        prem_str = f", {premium:.0f}% premium" if premium else ""
        return (
            f"{company} ({ticker}, {market}) is the target of an M&A "
            f"announcement: {size_str}{prem_str}."
        )

    return f"Event for {company}: {event}"


def get_ai_take(event: dict) -> str:
    """
    Returns a 1-line take on the event. Falls back to a template on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set, using fallback")
        return _fallback_take(event)

    summary = _build_event_summary(event)
    prompt = config.AI_TAKE_PROMPT_TEMPLATE.format(event_summary=summary)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=config.AI_TAKE_MODEL,
            max_tokens=config.AI_TAKE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        # Response content is a list of blocks; take the first text block
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        take = " ".join(text_blocks).strip()

        # Trim to ~140 chars to fit cleanly in iOS notifications
        if len(take) > 200:
            take = take[:197] + "..."
        return take

    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return _fallback_take(event)


def _fallback_take(event: dict) -> str:
    """Templated take for when the LLM is unavailable."""
    if event["source"] == "earnings":
        surprise = event.get("surprise_pct", 0)
        if surprise >= 10:
            return f"Strong {surprise:+.1f}% beat — classic drift setup, monitor for follow-through."
        if surprise >= 5:
            return f"{surprise:+.1f}% beat — modest, watch open price action."
        if surprise <= -10:
            return f"Sharp {surprise:+.1f}% miss — fade rally if any, expect drift down."
        return f"{surprise:+.1f}% surprise — within noise."

    if event["source"] in ("ma_us", "ma_uk"):
        return "Firm M&A offer — target typically pops to deal price minus deal-break risk."

    return "Event detected, fits monitoring criteria."

"""
ma_uk.py
────────
Detect UK M&A announcements from the London Stock Exchange RNS feed.

Background:
  RNS = Regulatory News Service. UK-listed companies must announce
  market-moving information here. M&A announcements specifically follow
  the City Code on Takeovers and Mergers, key sections being:
    - Rule 2.4: announcement of possible offer (talks, exploratory)
    - Rule 2.7: announcement of firm intention to make an offer
    - Rule 2.8: statement of no intention to bid

  Rule 2.7 is what we want — it's a binding commitment with deal terms.

How we get it:
  LSE provides an RNS feed at londonstockexchange.com/news. We can either
  scrape the HTML or use their JSON endpoint. We use a simpler approach:
  poll Investegate's free RNS aggregation feed which is more parser-friendly.

Note: Web scraping comes with risks — sites change layouts, sometimes
block scrapers. If LSE blocks us, the fallback is to use a paid news API
like Marketaux or Benzinga that includes RNS coverage.
"""
from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime

import requests

import config

log = logging.getLogger(__name__)


# Investegate's RNS RSS feed (free, no API key needed)
INVESTEGATE_RSS = "https://www.investegate.co.uk/Rss.aspx?type=h"

# Keywords that indicate a Rule 2.7 firm offer or completed acquisition
FIRM_OFFER_KEYWORDS = [
    "rule 2.7",
    "firm intention to make an offer",
    "recommended cash offer",
    "recommended offer",
    "announcement of offer",
    "scheme of arrangement",
]

POSSIBLE_OFFER_KEYWORDS = [
    "rule 2.4",
    "possible offer",
    "in discussions regarding",
    "approached regarding",
]


def _http_get(url: str) -> str:
    """Standard browser-like UA so we don't get blocked."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; SignalPipeline/1.0; "
            "+personal trading research tool)"
        ),
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def _parse_uk_deal_value(text: str) -> float | None:
    """
    Extract deal value from RNS text. Returns value in GBP.
    Patterns: '£250 million', '£1.2bn', '£500m', '500 million pounds'.
    """
    text_lower = text.lower()

    # £X billion
    bn = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*(?:billion|bn)", text_lower)
    if bn:
        return float(bn.group(1).replace(",", "")) * 1_000_000_000

    # £X million
    mn = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*(?:million|mn|m\b)", text_lower)
    if mn:
        return float(mn.group(1).replace(",", "")) * 1_000_000

    return None


def _classify_announcement(title: str, summary: str) -> str | None:
    """
    Returns 'firm', 'possible', or None.
    """
    combined = (title + " " + summary).lower()

    if any(kw in combined for kw in FIRM_OFFER_KEYWORDS):
        return "firm"
    if any(kw in combined for kw in POSSIBLE_OFFER_KEYWORDS):
        return "possible"
    return None


def fetch_uk_ma() -> list[dict]:
    """
    Returns a list of UK M&A event dicts from RNS.
    """
    log.info("Fetching Investegate RNS feed")

    try:
        import feedparser
        feed_text = _http_get(INVESTEGATE_RSS)
        feed = feedparser.parse(feed_text)
    except Exception as e:
        log.error(f"Failed to fetch RNS feed: {e}")
        return []

    log.info(f"RNS feed has {len(feed.entries)} recent items")

    events = []
    for entry in feed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        link = entry.get("link", "")

        offer_type = _classify_announcement(title, summary)
        if offer_type is None:
            continue

        # Skip possible offers if we only want firm offers
        if config.MA_REQUIRE_FIRM_OFFER and offer_type == "possible":
            continue

        # Try to extract deal value
        deal_value_gbp = _parse_uk_deal_value(title + " " + summary)
        deal_value_usd = deal_value_gbp * 1.27 if deal_value_gbp else None  # rough FX

        if deal_value_gbp is not None and deal_value_gbp < config.MA_MIN_DEAL_SIZE_GBP:
            log.debug(f"UK deal too small: {title}")
            continue

        # Extract ticker from RNS title — Investegate prefixes with the ticker
        # in a format like "BRBY: Burberry Group plc - ..."
        ticker_match = re.match(r"^([A-Z]{2,4}):\s*(.+)", title)
        ticker = ticker_match.group(1) if ticker_match else ""
        company_name = ticker_match.group(2) if ticker_match else title

        # Use the link as the unique ID since RNS doesn't have a clean accession
        event_id_safe = re.sub(r"\W+", "_", link)[-50:]

        events.append({
            "event_id": f"MA_UK_{event_id_safe}",
            "source": "ma_uk",
            "ticker": ticker,
            "company_name": company_name,
            "market": "UK",
            "market_cap_usd": None,
            "event_time": entry.get("published", datetime.utcnow().isoformat()),
            "surprise_pct": None,
            "deal_size_usd": deal_value_usd,
            "deal_premium": None,  # v2: fetch prior close, calculate
            "raw_data": json.dumps({
                "title": title,
                "summary": summary,
                "link": link,
                "offer_type": offer_type,
            }),
        })

    log.info(f"Found {len(events)} qualifying UK M&A announcements")
    return events

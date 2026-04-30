"""
ma_us.py
────────
Detect US M&A announcements by polling the SEC EDGAR full-text search for
8-K filings with M&A-related items.

Why 8-K filings:
  When a US public company has a material event (acquisition, merger,
  major contract), they MUST file an 8-K with the SEC within 4 business
  days. M&A specifically maps to:
    - Item 1.01: Entry into a Material Definitive Agreement
    - Item 2.01: Completion of Acquisition or Disposition of Assets

How we get it:
  EDGAR exposes a free RSS feed of recent filings. We pull the feed,
  filter for 8-Ks with the relevant items, then fetch the filing text
  to extract deal details (target, deal value, premium).

Note: SEC requires a User-Agent header identifying the requester.
Set SEC_USER_AGENT in config.py with your real email.
"""
from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime

import requests
import feedparser

import config

log = logging.getLogger(__name__)


# EDGAR's RSS for recent 8-K filings
EDGAR_8K_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&company=&datea=&dateb="
    "&owner=include&count=40&output=atom"
)


def _http_get(url: str) -> str:
    """SEC requires a real User-Agent identifying the caller."""
    headers = {
        "User-Agent": config.SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def _parse_deal_value(text: str) -> float | None:
    """
    Crude regex to extract a deal value from filing text.
    Patterns we look for:
        "for approximately $1.5 billion"
        "valued at $250 million"
        "$2.3B in cash"
        "consideration of $500,000,000"
    Returns the value in USD, or None if we can't find one.

    This is intentionally simple — for production you'd want a proper NLP
    parser, but for our purposes catching the obvious cases is enough.
    Anything we miss just doesn't trigger a notification, which is fine.
    """
    text_lower = text.lower()

    # Pattern: $X billion / $X bn / $XB
    billion_match = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:billion|bn|b\b)",
        text_lower,
    )
    if billion_match:
        return float(billion_match.group(1).replace(",", "")) * 1_000_000_000

    # Pattern: $X million / $X mn / $XM
    million_match = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|mn|m\b)",
        text_lower,
    )
    if million_match:
        return float(million_match.group(1).replace(",", "")) * 1_000_000

    # Pattern: $X,XXX,XXX,XXX
    raw_match = re.search(r"\$\s*([\d,]{8,})", text)
    if raw_match:
        try:
            return float(raw_match.group(1).replace(",", ""))
        except ValueError:
            pass

    return None


def _is_ma_filing(entry) -> tuple[bool, str]:
    """
    Heuristic check: does this 8-K look like an M&A announcement?

    Returns (is_ma, reason). EDGAR's RSS title for 8-Ks usually includes
    the items, e.g. "8-K - Item 1.01" — we look for those + M&A keywords
    in the summary.
    """
    title = (entry.get("title") or "").lower()
    summary = (entry.get("summary") or "").lower()
    combined = title + " " + summary

    # Item-based check
    if "item 1.01" in combined or "item 2.01" in combined:
        # Must also mention M&A keywords to filter out non-M&A material agreements
        ma_keywords = [
            "acquisition", "acquire", "acquired", "merger", "tender offer",
            "to be acquired", "purchase agreement",
        ]
        if any(kw in combined for kw in ma_keywords):
            return True, "8-K Item 1.01/2.01 + M&A keywords"

    return False, ""


def fetch_us_ma() -> list[dict]:
    """
    Returns a list of US M&A event dicts.

    Each dict has the standard signal fields plus deal_size_usd.
    """
    log.info("Fetching SEC EDGAR 8-K feed")

    try:
        feed_text = _http_get(EDGAR_8K_RSS)
    except Exception as e:
        log.error(f"Failed to fetch EDGAR feed: {e}")
        return []

    feed = feedparser.parse(feed_text)
    log.info(f"EDGAR feed has {len(feed.entries)} recent 8-Ks")

    events = []
    for entry in feed.entries:
        is_ma, reason = _is_ma_filing(entry)
        if not is_ma:
            continue

        # Extract accession number for dedup
        # EDGAR URLs look like: https://www.sec.gov/.../0001234567-25-000001-index.htm
        link = entry.get("link", "")
        accession_match = re.search(r"(\d{10}-\d{2}-\d{6})", link)
        if not accession_match:
            log.debug(f"Couldn't extract accession from {link}")
            continue
        accession = accession_match.group(1)

        # Extract ticker from title (EDGAR titles often include "(CIK 0000...)")
        # We'll need to fetch the actual filing for ticker — for now use the
        # company name from the title.
        title = entry.get("title", "")
        company_match = re.search(r"-\s*(.+?)\s*\(", title)
        company_name = company_match.group(1) if company_match else title

        # Try to fetch the filing text and extract deal value
        deal_value = None
        try:
            filing_text = _http_get(link)
            deal_value = _parse_deal_value(filing_text)
        except Exception as e:
            log.warning(f"Failed to fetch filing {link}: {e}")

        # Apply minimum deal size filter
        if config.MA_REQUIRE_FIRM_OFFER and deal_value is None:
            log.debug(f"No deal value parsed for {company_name}, skipping")
            continue
        if deal_value is not None and deal_value < config.MA_MIN_DEAL_SIZE_USD:
            log.debug(f"{company_name}: deal value ${deal_value/1e6:.0f}m below threshold")
            continue

        events.append({
            "event_id": f"MA_US_{accession}",
            "source": "ma_us",
            "ticker": "",  # would need a CIK→ticker lookup; left empty for v1
            "company_name": company_name,
            "market": "US",
            "market_cap_usd": None,
            "event_time": entry.get("updated", datetime.utcnow().isoformat()),
            "surprise_pct": None,
            "deal_size_usd": deal_value,
            "deal_premium": None,  # would need prior close price; v2 enhancement
            "raw_data": json.dumps({
                "title": title,
                "link": link,
                "summary": entry.get("summary", ""),
                "reason": reason,
            }),
        })

    log.info(f"Found {len(events)} qualifying US M&A filings")
    return events

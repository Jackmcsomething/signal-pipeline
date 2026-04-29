"""
Signal Pipeline - source modules.

Each file in this directory handles one concern:
  database.py  - SQLite storage and dedup
  earnings.py  - Finnhub earnings beat detection
  ma_us.py     - SEC EDGAR M&A scraping
  ma_uk.py     - LSE RNS M&A scraping
  scoring.py   - dissertation-style +1/0/-1 scoring
  ai_take.py   - Claude one-line analysis
  notify.py    - Pushover notification sender
"""

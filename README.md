# Signal Pipeline

A personal trading signal tool that monitors **earnings beats** and **M&A announcements** in real-time across **UK and US markets**, then sends scored alerts to your phone via Pushover.

Built for the 1-3 day post-announcement reaction window identified in dissertation research on news sentiment and price movements.

---

## What it does

Every 5 minutes during market hours, the pipeline:

1. **Pulls earnings data** from Finnhub (actual EPS vs consensus estimates)
2. **Pulls US M&A filings** from SEC EDGAR (8-K filings, Items 1.01 and 2.01)
3. **Pulls UK M&A announcements** from LSE RNS (Rule 2.7 firm offers)
4. **Filters** for events meeting your thresholds (size, surprise magnitude, deal certainty)
5. **Scores** each event using a +1 / 0 / -1 dissertation-style framework
6. **Generates a one-line take** via Claude API on whether the event fits the pattern
7. **Sends a Pushover notification** to your phone with all the above
8. **Logs everything** to SQLite so you can review history and (later) track performance

You tap the notification → see the details → open Trading 212 → buy or sell.

---

## Project structure

```
signal-pipeline/
├── README.md                  ← you are here
├── requirements.txt           ← Python dependencies
├── config.py                  ← all your tunable settings (thresholds, filters)
├── run.py                     ← the main entry point - run this
├── src/
│   ├── __init__.py
│   ├── database.py            ← SQLite setup and helpers
│   ├── earnings.py            ← Finnhub earnings beat detection
│   ├── ma_us.py               ← SEC EDGAR M&A scraping
│   ├── ma_uk.py               ← LSE RNS M&A scraping
│   ├── scoring.py             ← dissertation-style +1/0/-1 logic
│   ├── ai_take.py             ← Claude one-line analysis
│   └── notify.py              ← Pushover notification sender
├── tests/
│   └── test_local.py          ← run this locally before deploying
├── .github/
│   └── workflows/
│       └── pipeline.yml       ← GitHub Actions schedule
└── .gitignore
```

---

## Setup (one-time, ~15 minutes)

### 1. Create accounts and get API keys

| Service | What for | Cost | Where |
|---|---|---|---|
| **Finnhub** | Earnings data | Free | finnhub.io → sign up → API key in dashboard |
| **Pushover** | Phone notifications | £4 one-off | pushover.net → sign up → note your *User Key*, then create an *Application* and note its *API Token* |
| **Anthropic** | One-line AI takes | Pay-as-you-go (pennies/month at this volume) | console.anthropic.com → API Keys |
| **GitHub** | Hosting + scheduling | Free | github.com (you probably have one) |

### 2. Install Pushover on your phone

App Store / Play Store → "Pushover Notifications" → log in. Done.

### 3. Clone this repo and add your keys locally

```bash
git clone <your-repo-url>
cd signal-pipeline
cp .env.example .env
# edit .env with your keys
pip install -r requirements.txt
```

### 4. Test locally

```bash
python tests/test_local.py
```

This runs one full cycle and sends a test notification to your phone. If you get a ping, it works.

### 5. Push to GitHub and add secrets

In your GitHub repo → Settings → Secrets and variables → Actions → New repository secret. Add:

- `FINNHUB_API_KEY`
- `PUSHOVER_USER_KEY`
- `PUSHOVER_APP_TOKEN`
- `ANTHROPIC_API_KEY`

### 6. Enable the workflow

GitHub repo → Actions tab → enable workflows. The pipeline now runs every 5 minutes during market hours automatically.

---

## How to edit it

Almost everything you'll want to change lives in **`config.py`**. Open it and you'll see clearly labelled settings for:

- Earnings beat threshold (currently 5% to notify, 10% for "high conviction")
- M&A minimum deal size (currently £100m / $100m)
- Minimum market cap for stock universe (currently £500m / $500m)
- Polling frequency
- Tickers to ignore (e.g., if you don't want crypto-related stocks)
- Notification wording template

Change a number → commit → push → next run uses the new logic. No redeploy needed.

---

## Roadmap

**v1 (this build):** Detect, score, notify.

**v2 (later):** Performance tracker — for each signal sent, automatically log the entry price and check 1/2/3 day returns. Validates whether the signals actually work.

**v3 (later, for portfolio):** Web dashboard hosted on Vercel showing the signal feed, win rate, and average return per signal type. Good talking point for grad applications.

---

## Costs

- Pushover: £4 one-off
- Finnhub: free tier covers it
- SEC EDGAR / LSE RNS: free
- Anthropic API: ~£0.50/month at expected volume
- GitHub Actions: free (well within free tier)
- **Total: £4 setup + ~£0.50/month**

---

## Safety / legal note

This tool acts on **public information** released through official channels (RNS, SEC, earnings calendars). Acting on public announcements for personal trading is fine. If you ever want to share signals with others or charge for them, FCA rules around investment advice apply — talk to a compliance person first. For personal use and as a portfolio piece showing methodology, you're well clear.

# Market Data Collector

> Automated market data pipeline — fetches OHLCV, technical indicators, macro data, and sector flows on schedule. Designed to pair with a **local personal agent** for privacy-preserving portfolio tracking.

**Your data stays local.** This repo only stores publicly available market data. Your actual holdings, transaction history, and P&L never leave your PC.

---

## What This Does

Every trading day (even when your PC is off):

| Time (Beijing) | Action |
|----------------|--------|
| 15:10 | Fetch A-share close data (via efinance) |
| 16:10 | Fetch HK stock close data (via efinance) |
| 05:30 (next day) | Fetch US stock close data (via yfinance) |
| Saturday 08:00 | Weekly fundamentals snapshot (PE, PB, dividend yield) |

For each symbol, the pipeline:
1. Fetches OHLCV data (last 120 trading days, forward-adjusted)
2. Pre-computes 6 technical indicators (RSI, MACD, KDJ, Bollinger, MA, Volume)
3. Calculates Fibonacci retracement levels
4. Identifies support/resistance levels
5. Determines trend strength and overall resonance

Also collected daily:
- **Macro indicators**: VIX, 10Y Treasury Yield, KOSPI, Brent Crude, DXY, Shanghai Composite, Hang Seng Index
- **Sector fund flows**: Top 20 sectors by capital inflow (A-share market)

---

## Architecture

```
┌──────────────────────────────────────────┐
│  GitHub Actions (24/7, free tier)         │
│  ┌────────┐  ┌──────────┐  ┌──────────┐  │
│  │ fetch  │→ │indicators│→ │commit &  │  │
│  │ quotes │  │  compute │  │push data │  │
│  └────────┘  └──────────┘  └──────────┘  │
└──────────────────────────────────────────┘
                    │
            git pull (when you turn on your PC)
                    ▼
┌──────────────────────────────────────────┐
│  Your Local PC                            │
│  ┌──────────────┐  ┌───────────────────┐  │
│  │ market_data/ │ + │ trading.db        │  │
│  │ (public)     │   │ (private, local)  │  │
│  └──────────────┘  └───────────────────┘  │
│           │                │              │
│           └──────┬─────────┘              │
│                  ▼                        │
│       obsidian_holdings.py                │
│       (merge → generate dashboard)        │
└──────────────────────────────────────────┘
```

---

## Quick Start (For You)

### Prerequisites
- A GitHub account
- Python 3.10+ (only if you want to run scripts locally)

### 1. Fork or use this repo as template

This repo is already set up for you. If someone else wants to use it:

```bash
# Option A: Fork via GitHub UI
# Visit https://github.com/alex00ccc-lab/market-data-collector → Fork

# Option B: Use as template
gh repo create my-market-data --template alex00ccc-lab/market-data-collector --private
```

### 2. Edit symbol lists

Edit these three files to match your portfolio:

**`config/holdings.json`** — Stocks you currently hold:
```json
{
  "symbols": [
    {"symbol": "600519.SH", "market": "A", "name": "贵州茅台", "sector": "消费"},
    {"symbol": "AAPL", "market": "US", "name": "Apple", "sector": "消费电子"}
  ]
}
```

**`config/watchlist.json`** — Stocks you're watching but haven't bought:
```json
{
  "symbols": [
    {"symbol": "NVDA", "market": "US", "name": "NVIDIA", "sector": "半导体"},
    {"symbol": "00700.HK", "market": "HK", "name": "腾讯", "sector": "互联网"}
  ]
}
```

**`config/macro.json`** — Macro indicators (preset, can be customized)

### 3. Enable GitHub Actions

1. Go to your repo → **Settings** → **Actions** → **General**
2. Under "Actions permissions", select **Allow all actions**
3. Under "Workflow permissions", select **Read and write permissions**

### 4. Trigger first fetch

Go to **Actions** tab → **Daily Market Data Fetch** → **Run workflow** → check **Force fetch** → **Run workflow**

After ~2 minutes, check the `data/` directory — you should see today's date folder with quote and indicator JSONs.

### 5. Connect to local agent

Clone this repo into your local personal agent's data directory:

```bash
cd /path/to/your/personal_agent/data/
git clone git@github.com:your-username/market-data-collector.git market_data
```

Your local `obsidian_holdings.py` reads from `data/market_data/{date}/` to populate the dashboard.

---

## Data Format

### Quotes (`data/{date}/quotes/{SYMBOL}.json`)
```json
[
  {
    "date": "2026-06-09",
    "open": 16.80,
    "close": 16.90,
    "high": 17.10,
    "low": 16.60,
    "volume": 1234567.0,
    "amount": 21000000.0,
    "adj": "qfq",
    "source": "efinance"
  }
]
```

### Indicators (`data/{date}/indicators/{SYMBOL}.json`)
```json
{
  "symbol": "0189HK",
  "date": "2026-06-09",
  "close": 16.90,
  "ma": {"ma5": 16.85, "ma10": 16.72, "ma20": 16.55, "alignment": "mixed"},
  "rsi": {"value": 52.3, "signal": "bullish"},
  "macd": {"diff": 0.15, "dea": 0.12, "histogram": 0.06, "signal": "golden_cross"},
  "bollinger": {"upper": 17.80, "middle": 16.55, "lower": 15.30, "bandwidth": 15.1, "position": 0.64},
  "fibonacci": {"high": 18.20, "low": 15.20, "50.0%": 16.70},
  "trend_strength": "震荡",
  "supports": [15.80],
  "resistances": [17.50, 18.20],
  "resonance": {"bullish": 3, "neutral": 2, "bearish": 1, "overall": "mildly_bullish"}
}
```

### Macro (`data/{date}/macro.json`)
```json
{
  "^VIX": {"name": "VIX 恐慌指数", "price": 18.5, "date": "2026-06-09", "change_pct": -3.2},
  "^TNX": {"name": "10年期美债收益率", "price": 4.35, "date": "2026-06-09", "change_pct": 0.5}
}
```

---

## Privacy Design

| Data | Stored in this repo | Leaves your PC |
|------|---------------------|----------------|
| Stock symbols (tickers) | ✅ Yes | ✅ To GitHub (public tickers) |
| OHLCV market data | ✅ Yes | ✅ To GitHub (public data) |
| Technical indicators | ✅ Yes | ✅ To GitHub (computed from public data) |
| Your holdings (qty, cost) | ❌ No | ❌ Never |
| Your transaction history | ❌ No | ❌ Never |
| Your P&L | ❌ No | ❌ Never |
| Your sentiment scores | ❌ No | ❌ Never |
| Your API keys | ❌ No | ❌ Never |

**What's visible in this repo**: Just a list of stock tickers you're interested in, plus publicly available market data. Anyone can get this data from Yahoo Finance or Eastmoney for free.

---

## Customization

### Adding a new market
Edit `scripts/fetch.py` and add market handling in `fetch_all()`. The architecture supports any data source — just implement a fetcher function and register it.

### Changing fetch schedule
Edit `.github/workflows/fetch-daily.yml` — the `cron` lines. Use [crontab.guru](https://crontab.guru) to test expressions.

### Adding custom indicators
Edit `scripts/indicators.py` — add a `calc_*()` function and wire it into `compute_all()`. Pure Python math, no external dependencies needed.

### Using with a different local agent
The data format is standard JSON. Any tool that can read JSON files can consume this data. You don't need the `personal_agent` project — write your own consumer in any language.

---

## Obsidian Export

The repository now includes an exporter script for Obsidian-compatible snapshots.
Run from `market_data`:

```bash
python scripts/export_to_obsidian.py --date YYYY-MM-DD
```

This writes markdown output to `market_data/data/{date}/obsidian.md` and optionally to a configured Obsidian vault path if your environment is set up.

---

## API Sources

| Source | Market | Cost | Rate Limit |
|--------|--------|------|------------|
| [efinance](https://github.com/Micro-sheep/efinance) | A-shares, HK | **Free** | No official limit (use RateLimiter) |
| [yfinance](https://github.com/ranaroussi/yfinance) | US, Indices | **Free** | Moderate (RateLimiter 500ms) |
| [Stooq](https://stooq.com) | US, some global tickers | **Free** | No API key required, public CSV |

No API keys required for these sources. All use public HTTP endpoints.

---

## Troubleshooting

### "efinance HTTP error" in logs
- Eastmoney API may rate-limit temporarily. The retry decorator handles this — 2 attempts with 1s delay.
- If persistent, check if the symbol code format is correct (A-shares need `.SH` or `.SZ` suffix).

### "yfinance history() returned None"
- US market data may not be available immediately after close. Wait 30-60 min.
- Some tickers require exact Yahoo Finance format (e.g., `BZ=F` not `BZ`).

### Manual trigger
Go to **Actions** → **Daily Market Data Fetch** → **Run workflow** → **Force fetch**.

### Data not updating
- Check **Settings** → **Actions** → **General** → "Workflow permissions" = "Read and write"
- Check if today is a trading day (script auto-skips holidays)
- Check workflow run logs for errors

---

## For Other Users

This project is designed to be **self-serve**:

1. **Fork this repo** (make it private if you prefer)
2. **Edit the three config files** in `config/` to match your portfolio
3. **Enable GitHub Actions** (Settings → Actions → Allow)
4. **Clone to your local machine** and point your agent at `data/`

You bring:
- Your own stock watchlists
- Your own local agent / dashboard system
- Your own LLM API key (for weekly analysis — entirely separate from this repo)

No shared infrastructure. No shared API keys. No shared data. Each user's repo is fully self-contained.

---

## License

MIT — Feel free to use, modify, and share.

---

*Built for the [personal_agent](https://github.com/alex00ccc-lab) ecosystem. Data pipeline runs on GitHub Actions free tier (~132 min/month usage).*

"""Yahoo Finance Chart API (v8) adapter — fallback for yfinance.

Uses ``query2.finance.yahoo.com/v8/finance/chart`` with cookie+crumb auth.
Same IP rate-limit pool as yfinance, but the chart endpoint may have
independent availability windows.  Zero API key required.

NOTE: If your IP is hard-blocked by Yahoo, this adapter will fail too.
      In that case, use a keyed adapter (Finnhub, Twelve Data) instead.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from .base import BaseAdapter

logger = logging.getLogger(__name__)
TZ_BEIJING = timezone(timedelta(hours=8))

# Rate limiting (chart endpoint — same IP pool as yfinance, but separate interval)
_last_call = 0.0
_MIN_INTERVAL = 2.0

# Session + crumb cache (refreshed every 30 min or on 401)
_session: Optional[requests.Session] = None
_crumb: Optional[str] = None
_crumb_ts: float = 0.0
_CRUMB_TTL = 1800  # 30 minutes


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


def _get_session() -> tuple[requests.Session, Optional[str]]:
    """Return a requests.Session with valid Yahoo cookie and crumb.

    Caches the session for _CRUMB_TTL seconds to avoid re-auth on every call.
    """
    global _session, _crumb, _crumb_ts

    now = _time.time()
    if _session is not None and _crumb and (now - _crumb_ts) < _CRUMB_TTL:
        return _session, _crumb

    _session = requests.Session()
    _session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })

    try:
        # Step 1: get cookie from Yahoo Finance
        _session.get("https://finance.yahoo.com/", timeout=15)

        # Step 2: get crumb
        resp = _session.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            timeout=10,
        )
        if resp.status_code == 200 and resp.text.strip():
            _crumb = resp.text.strip()
            _crumb_ts = now
            logger.debug("yahoo_chart: crumb refreshed")
            return _session, _crumb
    except Exception as e:
        logger.debug("yahoo_chart: auth failed — %s", str(e)[:80])

    _crumb = None
    return _session, None


class YahooChartAdapter(BaseAdapter):
    """Fetches OHLCV via Yahoo Finance Chart API (v8).

    Falls back when yfinance is rate-limited on its primary history endpoint.
    Both share the same IP, so this is NOT a hard block workaround — use a
    different provider (Finnhub, Alpha Vantage) for that.
    """

    @property
    def name(self) -> str:
        return "yahoo_chart"

    def supports_market(self, market: str) -> bool:
        return market in ("US", "JP", "HK")

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        # Build Yahoo ticker suffix
        yf_sym = symbol
        if market == "JP" and not symbol.endswith(".T"):
            yf_sym = f"{symbol}.T"
        elif market == "HK" and not symbol.endswith(".HK"):
            yf_sym = f"{symbol}.HK"

        # Map days → Yahoo range param
        if days <= 5:
            range_ = "5d"
        elif days <= 30:
            range_ = "1mo"
        else:
            range_ = "3mo"

        session, crumb = _get_session()
        if not crumb:
            logger.debug("yahoo_chart(%s): skipped — no valid crumb", symbol)
            return None

        url = (
            f"https://query2.finance.yahoo.com/v8/finance/chart/{yf_sym}"
            f"?range={range_}&interval=1d&crumb={crumb}"
        )

        _rate_limit()
        try:
            resp = session.get(url, timeout=15)
        except Exception as e:
            logger.debug("yahoo_chart(%s): request error — %s", symbol, str(e)[:80])
            return None

        if resp.status_code == 401:
            # Crumb expired — invalidate cache so next call refreshes
            global _crumb_ts
            _crumb_ts = 0.0
            logger.debug("yahoo_chart(%s): crumb expired, will refresh", symbol)
            return None

        if resp.status_code != 200:
            logger.debug("yahoo_chart(%s): HTTP %d", symbol, resp.status_code)
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        r = result[0]
        timestamps = r.get("timestamp", [])
        quotes = r.get("indicators", {}).get("quote", [{}])[0]

        if not timestamps or not quotes:
            return None

        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])

        bars = []
        for i, ts in enumerate(timestamps):
            if closes[i] is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(ts, tz=TZ_BEIJING).strftime("%Y-%m-%d"),
                "open": float(opens[i]) if opens[i] is not None else 0.0,
                "high": float(highs[i]) if highs[i] is not None else 0.0,
                "low": float(lows[i]) if lows[i] is not None else 0.0,
                "close": float(closes[i]),
                "volume": int(volumes[i]) if volumes[i] is not None else 0,
                "source": "yahoo_chart",
            })

        if bars:
            logger.info("yahoo_chart(%s) OK — %d bars", symbol, len(bars))
        return bars if bars else None

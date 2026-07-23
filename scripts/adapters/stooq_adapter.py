"""Stooq adapter — free CSV-based OHLCV data, no API key required."""

from __future__ import annotations

import logging
import urllib.request
from typing import Optional

from .base import BaseAdapter

logger = logging.getLogger(__name__)

# Minimal rate limiting
import time as _time
_last_call = 0.0


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < 1.0:
        _time.sleep(1.0 - elapsed)
    _last_call = _time.time()


class StooqAdapter(BaseAdapter):
    """Fetches daily OHLCV from Stooq CSV endpoint.

    Stooq uses lowercase symbols with market-specific suffixes:
      US → {ticker}.us   JP → {ticker}.jp
      HK → {ticker}.hk   A  → {ticker}.sh
    """

    @property
    def name(self) -> str:
        return "stooq"

    def supports_market(self, market: str) -> bool:
        return market in ("US", "JP", "HK", "A")

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        suffix_map = {"US": ".us", "JP": ".jp", "HK": ".hk", "A": ".sh"}

        # Build candidate stooq symbol forms
        cand = []
        s = symbol.strip()
        if "." in s:
            base, _ = s.split(".", 1)
            cand.append(base.lower())
            msuf = suffix_map.get(market, ".us")
            cand.append(f"{base.lower()}{msuf}")
        else:
            msuf = suffix_map.get(market, ".us")
            cand.append(f"{s.lower()}{msuf}")
            cand.append(s.lower())
            if market == "US":
                cand.append(f"{s.lower()}.usd")

        headers = {"User-Agent": "Mozilla/5.0"}

        for c in cand:
            url = f"https://stooq.com/q/d/l/?s={c}&i=d"
            _rate_limit()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    text = resp.read().decode("utf-8-sig")
            except Exception as e:
                logger.debug("stooq(%s): request error — %s", c, str(e)[:80])
                continue

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines or lines[0].lower().startswith("no data"):
                continue

            rows = []
            for ln in lines[1:]:
                parts = ln.split(",")
                if len(parts) < 6:
                    continue
                try:
                    dt = parts[0]
                    open_p = float(parts[1])
                    high_p = float(parts[2])
                    low_p = float(parts[3])
                    close_p = float(parts[4])
                    vol = int(float(parts[5])) if parts[5] not in ("", "-") else 0
                except (ValueError, IndexError):
                    continue
                rows.append({
                    "date": dt,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": vol,
                    "source": "stooq",
                })

            if rows:
                logger.info("stooq(%s) OK via '%s' — %d rows", symbol, c, len(rows))
                return rows

        logger.debug("stooq(%s): all candidates failed: %s", symbol, cand)
        return None

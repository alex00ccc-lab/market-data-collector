"""Finnhub adapter — independent third-party US stock data (60 calls/min free).

Completely separate from Yahoo's IP pool — serves as the primary escape hatch
when Yahoo rate-limits the GitHub Actions IP.

⚠️ 免费档限制（实测 2026-08-14）：finnhub 免费档**不含 `/stock/candle`（历史 OHLCV）**
——`fetch_kline` 返回 403 "You don't have access to this resource"。免费档仅覆盖
quote/company_profile2/news 等实时/基本面端点。故本 adapter 的 fetch_kline 在免费档下
恒返回 None（降级到 twelvedata/alpha_vantage）；只有升级付费档后才真正具备 OHLCV 逃生舱能力。
key 本身有效（`/quote` 实测正常返回），可用于 fetch_realtime / fetch_fundamentals 扩展。
"""

from __future__ import annotations

import json
import logging
import urllib.request
import time as _time
from datetime import datetime
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, HTTP_TIMEOUT, LOG_TRUNCATE_LENGTH
from utils.http_utils import random_ua
from utils.exceptions import RateLimitError, KeyInvalidError

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 1.1  # 60 calls/min → ~1s interval with safety margin
_MAX_BARS = 120       # max K-line bars to return (memory guard)


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


class FinnhubAdapter(BaseAdapter):
    """Fetches OHLCV from Finnhub REST API.

    Free tier: 60 API calls/minute. Requires API key (free registration).
    US stocks only in free tier.
    """

    @property
    def name(self) -> str:
        return "finnhub"

    def supports_market(self, market: str) -> bool:
        return market == "US"

    def _get_key(self) -> str:
        from key_loader import get_key
        return get_key("finnhub_api_key", "")

    def _is_available(self) -> bool:
        return bool(self._get_key())

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        api_key = self._get_key()
        if not api_key:
            raise KeyInvalidError("finnhub: API key not configured")

        if self.is_rate_limited():
            logger.debug("finnhub(%s): skipped — in cooldown", symbol)
            return None

        # Cap bars to prevent memory bloat
        days = min(days, _MAX_BARS)

        # Finnhub needs Unix timestamps for from/to
        to_ts = int(datetime.now(TZ_BEIJING).timestamp())
        from_ts = to_ts - days * 86400 * 2  # generous window for trading days

        url = (
            f"https://finnhub.io/api/v1/stock/candle"
            f"?symbol={symbol}&resolution=D"
            f"&from={from_ts}&to={to_ts}"
            f"&token={api_key}"
        )

        _rate_limit()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random_ua()})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                # Check for HTTP 429 (rate limit)
                if resp.status == 429:
                    self.set_cooldown(900)
                    logger.warning("finnhub(%s): HTTP 429 — rate-limited, cooling down", symbol)
                    raise RateLimitError(f"finnhub HTTP 429 on {symbol}")
                data = json.loads(resp.read().decode())
        except RateLimitError:
            raise
        except Exception as e:
            logger.debug("finnhub(%s): request error — %s", symbol, str(e)[:LOG_TRUNCATE_LENGTH])
            return None

        if data.get("s") != "ok":
            logger.debug("finnhub(%s): API status=%s", symbol, data.get("s", "unknown"))
            return None

        timestamps = data.get("t", [])
        opens = data.get("o", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        closes = data.get("c", [])
        volumes = data.get("v", [])

        if not timestamps:
            return None

        bars = []
        for i, ts in enumerate(timestamps):
            if closes[i] is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(ts, tz=TZ_BEIJING).strftime("%Y-%m-%d"),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": int(volumes[i]),
                "source": "finnhub",
            })

        if bars:
            logger.info("finnhub(%s) OK — %d bars", symbol, len(bars))
        return bars if bars else None

    def health_check(self) -> bool:
        """Quick connectivity + key validity test."""
        api_key = self._get_key()
        if not api_key:
            return False
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": random_ua()})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            return data.get("c", 0) > 0
        except Exception:
            return False

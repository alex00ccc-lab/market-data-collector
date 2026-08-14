"""Twelve Data adapter — independent third-party market data (800 calls/day free).

IMPORTANT (实测 2026-08-14): Twelve Data 免费档仅覆盖美股。东京 (JPX)、港交所
(HKEX)、台湾 (TWSE)、斯德哥尔摩 (XSTO) 均需 Pro/Venture 付费档。因此本 adapter
只注册 US 市场 —— 日股/欧股的独立 fallback 仍是缺口，需付费档或另寻免费独立源。
"""

from __future__ import annotations

import json
import logging
import time as _time
import urllib.request
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, HTTP_TIMEOUT, LOG_TRUNCATE_LENGTH
from utils.http_utils import random_ua
from utils.exceptions import RateLimitError, KeyInvalidError

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 1.1   # 800/day ≈ 每 108s 一次；突发时保守限速
_MAX_BARS = 200       # 单次返回 K 线上限（内存护栏）


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


class TwelveDataAdapter(BaseAdapter):
    """Fetches OHLCV from Twelve Data REST API (free tier = US only)."""

    @property
    def name(self) -> str:
        return "twelvedata"

    def supports_market(self, market: str) -> bool:
        # 免费档仅美股（见模块 docstring 实测结论）
        return market == "US"

    def _get_key(self) -> str:
        from key_loader import get_key
        return get_key("twelvedata_api_key", "")

    def _is_available(self) -> bool:
        return bool(self._get_key())

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        api_key = self._get_key()
        if not api_key:
            raise KeyInvalidError("twelvedata: API key not configured")

        if self.is_rate_limited():
            logger.debug("twelvedata(%s): skipped — in cooldown", symbol)
            return None

        outputsize = min(days, _MAX_BARS)
        url = (
            f"https://api.twelvedata.com/time_series"
            f"?symbol={symbol}&interval=1day"
            f"&outputsize={outputsize}"
            f"&apikey={api_key}"
        )

        _rate_limit()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random_ua()})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status == 429:
                    self.set_cooldown(900)
                    logger.warning("twelvedata(%s): HTTP 429 — rate-limited, cooling down", symbol)
                    raise RateLimitError(f"twelvedata HTTP 429 on {symbol}")
                data = json.loads(resp.read().decode())
        except RateLimitError:
            raise
        except Exception as e:
            logger.debug("twelvedata(%s): request error — %s", symbol, str(e)[:LOG_TRUNCATE_LENGTH])
            return None

        if data.get("status") == "error":
            code = data.get("code")
            msg = data.get("message", "")
            if code == 401:
                logger.warning("twelvedata: invalid API key — %s", msg[:LOG_TRUNCATE_LENGTH])
                raise KeyInvalidError(f"twelvedata key invalid: {msg[:60]}")
            # 400/404（含付费档市场）非限流 —— 不 cooldown，直接降级
            logger.debug("twelvedata(%s): API error %s — %s", symbol, code, msg[:LOG_TRUNCATE_LENGTH])
            return None

        values = data.get("values", [])
        if not values:
            return None

        bars = []
        for v in values:
            try:
                bars.append({
                    "date": str(v.get("datetime", ""))[:10],
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                    "volume": int(v.get("volume", 0) or 0),
                    "source": "twelvedata",
                })
            except (KeyError, ValueError, TypeError):
                continue

        # Twelve Data 最新在前 → 归一化为日期升序
        bars.sort(key=lambda b: b["date"])
        if bars:
            logger.info("twelvedata(%s) OK — %d bars", symbol, len(bars))
        return bars if bars else None

    def health_check(self) -> bool:
        api_key = self._get_key()
        if not api_key:
            return False
        try:
            url = (
                f"https://api.twelvedata.com/time_series"
                f"?symbol=AAPL&interval=1day&outputsize=1&apikey={api_key}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": random_ua()})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            return data.get("status") == "ok"
        except Exception:
            return False

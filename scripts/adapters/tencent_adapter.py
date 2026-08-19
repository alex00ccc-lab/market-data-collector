"""Tencent Finance adapter — free A-share / HK stock data via qt.gtimg.cn.

Independent of East Money (efinance/akshare) and Yahoo (yfinance).  Tencent
Finance serves A-shares and HK stocks from mainland-China CDNs without geo- or
residential-IP blocking, making it the primary source when East Money drops
connections to mainland residential IPs.

Endpoints:
  - realtime:   https://qt.gtimg.cn/q=<code>          (GBK, ``v_<code>="..."``)
  - history:    https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
                (GBK JSON-P, ``kline_dayqfq={...}``)

No API key required.
"""

from __future__ import annotations

import json
import logging
import time as _time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, LOG_TRUNCATE_LENGTH
from utils.exceptions import RateLimitError
from utils.http_utils import random_ua

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 0.2  # seconds — Tencent throttles on burst; keep ≥100ms


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


class TencentAdapter(BaseAdapter):
    """Fetches OHLCV and real-time quotes for A-shares and HK stocks via Tencent."""

    @property
    def name(self) -> str:
        return "tencent"

    def supports_market(self, market: str) -> bool:
        return market in ("A", "HK")

    def _tencent_code(self, symbol: str, market: str) -> str:
        """Build Tencent code from symbol (e.g. 513010.SH → sh513010, 9992.HK → hk09992)."""
        code = symbol.upper().strip()
        if market == "HK":
            return "hk" + code.replace(".HK", "").zfill(5)
        if ".SH" in code:
            return "sh" + code.replace(".SH", "")
        if ".SZ" in code:
            return "sz" + code.replace(".SZ", "")
        # No suffix: infer exchange from leading digit (6/9/5 → SH, else SZ).
        if code[:1] in ("6", "9", "5"):
            return "sh" + code
        return "sz" + code

    def _http_get(self, url: str, timeout: int = 15) -> Optional[str]:
        """GET a Tencent endpoint, returning the raw text (GBK-decoded).

        Returns None on network errors; raises RateLimitError on HTTP 403/429
        (Tencent throttles bursts with 403 / connection resets) so SourceManager
        can set a cooldown and fall through.
        """
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random_ua()})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("gbk", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise RateLimitError(
                    f"tencent HTTP {e.code} (rate-limited): {url[:60]}"
                ) from e
            logger.debug("tencent HTTP %s: %s", e.code, str(e)[:LOG_TRUNCATE_LENGTH])
            return None
        except Exception as e:
            logger.debug("tencent HTTP: %s", str(e)[:LOG_TRUNCATE_LENGTH])
            return None

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        tcode = self._tencent_code(symbol, market)
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq&param={tcode},day,,,{days},qfq"
        )

        _rate_limit()
        text = self._http_get(url)
        if not text:
            return None

        # Response is JSON-P: ``kline_dayqfq={...}`` — strip to the JSON body.
        idx = text.find("{")
        if idx < 0:
            return None
        try:
            data = json.loads(text[idx:])
        except json.JSONDecodeError:
            logger.debug("tencent(%s): bad JSON-P", symbol)
            return None

        node = (data.get("data") or {}).get(tcode) or {}
        # 前复权日K (qfqday) for stocks; plain day for ETFs/HK. Fall back to day.
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            return None

        result = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            # Tencent bar order: [date, open, close, high, low, volume] —
            # NOTE close is index 2 (not 4). Verified against realtime cross-check.
            try:
                close = float(row[2])
            except (ValueError, TypeError):
                continue
            if close <= 0:
                continue
            result.append({
                "date": str(row[0])[:10],
                "open": float(row[1] or 0),
                "close": close,
                "high": float(row[3] or 0),
                "low": float(row[4] or 0),
                "volume": float(row[5] or 0),
                "source": "tencent",
            })

        if result:
            logger.info("tencent(%s) OK — %d bars", symbol, len(result))
        return result if result else None

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        tcode = self._tencent_code(symbol, market)
        url = f"https://qt.gtimg.cn/q={tcode}"

        _rate_limit()
        text = self._http_get(url)
        if not text:
            return None

        # Response: ``v_sh513010="1~name~code~price~...~";``
        idx = text.find('="')
        if idx < 0:
            return None
        content = text[idx + 2:]
        end = content.rfind('"')
        if end < 0:
            return None
        fields = content[:end].split("~")
        if len(fields) < 35:
            return None

        try:
            price = float(fields[3])
        except (ValueError, TypeError):
            return None
        if price <= 0:
            return None

        now = datetime.now(TZ_BEIJING)
        return {
            "symbol": symbol.upper(),
            "name": fields[1],
            "price": price,
            "change_pct": float(fields[32] or 0),   # already percent (e.g. -1.12)
            "high": float(fields[33] or 0),
            "low": float(fields[34] or 0),
            "open": float(fields[5] or 0),
            "pre_close": float(fields[4] or 0),
            "volume": float(fields[6] or 0),
            "source": "tencent",
            "timestamp": now.isoformat(),
            "trade_date": now.strftime("%Y-%m-%d"),
        }

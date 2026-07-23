"""efinance adapter — free A-share / HK stock data via East Money API.

Note: East Money APIs may block requests from non-Chinese IPs, so this
adapter is most useful when run from mainland China or via a CN proxy.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

from .base import BaseAdapter

logger = logging.getLogger(__name__)
TZ_BEIJING = timezone(timedelta(hours=8))

import time as _time
_last_call = 0.0


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < 0.5:
        _time.sleep(0.5 - elapsed)
    _last_call = _time.time()


class EFinanceAdapter(BaseAdapter):
    """Fetches OHLCV and real-time quotes for A-shares and HK stocks via efinance."""

    @property
    def name(self) -> str:
        return "efinance"

    def supports_market(self, market: str) -> bool:
        return market in ("A", "HK")

    def _secid(self, symbol: str, market: str) -> str:
        """Build East Money secid from symbol."""
        code = symbol.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")
        if market == "A":
            if code.startswith(("0", "3")):
                return f"0.{code}"
            return f"1.{code}"
        if market == "HK":
            return f"116.{code}"
        return f"1.{code}"

    def _http_get(self, url: str, timeout: int = 15) -> Optional[dict]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("efinance HTTP: %s", str(e)[:80])
            return None

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        secid = self._secid(symbol, market)
        url = (
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
            f"&klt=101&fqt=1&end=20500101&lmt={days}"
        )

        _rate_limit()
        data = self._http_get(url)
        if not data or "data" not in data or not data["data"]:
            return None

        klines = data["data"].get("klines", [])
        if not klines:
            return None

        result = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            result.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "source": "efinance",
            })
        return result

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        secid = self._secid(symbol, market)
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid}"
            f"&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f170"
        )

        _rate_limit()
        data = self._http_get(url)
        if not data or "data" not in data or not data["data"]:
            return None

        d = data["data"]
        price = d.get("f43", 0) / 100 if d.get("f43") else 0
        if price <= 0:
            return None

        now = datetime.now(TZ_BEIJING)
        return {
            "symbol": symbol.upper(),
            "name": d.get("f58", ""),
            "price": price,
            "change_pct": d.get("f170", 0) / 100 if d.get("f170") else 0,
            "high": d.get("f44", 0) / 100 if d.get("f44") else 0,
            "low": d.get("f45", 0) / 100 if d.get("f45") else 0,
            "open": d.get("f46", 0) / 100 if d.get("f46") else 0,
            "pre_close": d.get("f60", 0) / 100 if d.get("f60") else 0,
            "volume": d.get("f47", 0),
            "amount": d.get("f48", 0),
            "source": "efinance",
            "timestamp": now.isoformat(),
            "trade_date": now.strftime("%Y-%m-%d"),
        }

    def fetch_sector_flow(self) -> Optional[list[dict]]:
        """Fetch A-share sector fund flow rankings."""
        url = (
            "https://push2.eastmoney.com/api/qt/clt/get"
            "?fields=f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87"
            "&fid=f62&po=1&pz=20&np=1&fltt=2&invt=2"
        )
        _rate_limit()
        data = self._http_get(url)
        if not data or "data" not in data:
            return None
        entries = data["data"].get("diff", [])
        if not entries:
            return None
        result = []
        for e in entries[:20]:
            result.append({
                "code": e.get("f12", ""),
                "name": e.get("f14", ""),
                "net_inflow": e.get("f62", 0),
                "inflow_ratio": e.get("f66", 0),
                "change_pct": e.get("f69", 0) / 100 if e.get("f69") else 0,
            })
        return result

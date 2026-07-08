"""SourceManager — intelligent multi-source data fetching with health tracking.

Routes each symbol through a priority-ordered chain of adapters, falling back
when the primary source fails.  Tracks per-source health statistics so the
briefing engine can report data quality.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from adapters.base import BaseAdapter
from adapters.yfinance_adapter import YFinanceAdapter
from adapters.stooq_adapter import StooqAdapter
from adapters.efinance_adapter import EFinanceAdapter

logger = logging.getLogger(__name__)
TZ_BEIJING = timezone(timedelta(hours=8))

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# ---------------------------------------------------------------------------
# Alpha Vantage stub — activate when API key is available
# ---------------------------------------------------------------------------

class AlphaVantageAdapter(BaseAdapter):
    """Alpha Vantage free tier adapter (25 calls/day, 5 calls/min).

    Environment variable ``ALPHA_VANTAGE_API_KEY`` must be set.
    """

    @property
    def name(self) -> str:
        return "alpha_vantage"

    def supports_market(self, market: str) -> bool:
        return market == "US"

    def _is_available(self) -> bool:
        import os
        return bool(os.getenv("ALPHA_VANTAGE_API_KEY", "").strip())

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        if not self._is_available():
            return None

        import os
        import urllib.request

        api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
        # Use TIME_SERIES_DAILY (compact returns last 100 data points)
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=TIME_SERIES_DAILY"
            f"&symbol={symbol}"
            f"&outputsize=compact"
            f"&apikey={api_key}"
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning("alpha_vantage(%s): request error — %s", symbol, str(e)[:80])
            return None

        # Check for rate limit / error messages
        if "Note" in data:
            logger.warning("alpha_vantage(%s): rate limit — %s", symbol, data["Note"][:100])
            return None
        if "Error Message" in data:
            logger.warning("alpha_vantage(%s): API error — %s", symbol, data["Error Message"])
            return None

        ts = data.get("Time Series (Daily)", {})
        if not ts:
            logger.warning("alpha_vantage(%s): empty time series", symbol)
            return None

        result = []
        for date_str, values in sorted(ts.items())[-days:]:
            try:
                result.append({
                    "date": date_str,
                    "open": float(values["1. open"]),
                    "high": float(values["2. high"]),
                    "low": float(values["3. low"]),
                    "close": float(values["4. close"]),
                    "volume": float(values["5. volume"]),
                    "source": "alpha_vantage",
                })
            except (KeyError, ValueError):
                continue

        if result:
            logger.info("alpha_vantage(%s) OK — %d bars", symbol, len(result))
        return result if result else None


# ============================================================================
# SourceManager
# ============================================================================

class SourceManager:
    """Orchestrates multiple data adapters with priority-based fallback.

    Usage::

        mgr = SourceManager()
        kline = mgr.fetch_with_fallback("TSLA", "US")

    The manager tries each adapter in the configured priority order until one
    returns data.  Health statistics are tracked per-source and per-symbol.
    """

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}
        self._stats: dict[str, dict[str, Any]] = {}   # per-source health
        self._register_defaults()

    def _register_defaults(self):
        """Register all built-in adapters."""
        self.register(YFinanceAdapter())
        self.register(StooqAdapter())
        self.register(EFinanceAdapter())
        # Alpha Vantage is registered but will no-op until API key is set
        self.register(AlphaVantageAdapter())

    def register(self, adapter: BaseAdapter):
        self._adapters[adapter.name] = adapter

    def get_priority(self, market: str) -> list[str]:
        """Read priority order from config/sources.json, filtered to market."""
        cfg = self._load_sources_config()
        priority = cfg.get("priority", ["yfinance", "stooq"])
        # Filter to adapters that support this market
        return [
            name for name in priority
            if name in self._adapters and self._adapters[name].supports_market(market)
        ]

    def fetch_with_fallback(
        self,
        symbol: str,
        market: str,
        days: int = 120,
    ) -> Optional[list[dict]]:
        """Fetch OHLCV data, trying adapters in priority order.

        Returns:
            First successful kline data, or None if all adapters fail.
        """
        priority = self.get_priority(market)
        if not priority:
            logger.warning("No adapters registered for market=%s", market)
            return None

        for name in priority:
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            if not adapter.supports_market(market):
                continue

            kline = adapter.fetch_kline(symbol, market, days)
            if kline and len(kline) > 0:
                self._record(name, symbol, "ok", len(kline))
                return kline
            else:
                self._record(name, symbol, "failed", 0)

        return None

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        """Try to get a real-time quote from the first available adapter."""
        priority = self.get_priority(market)
        for name in priority:
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            result = adapter.fetch_realtime(symbol, market)
            if result:
                return result
        return None

    def fetch_fundamentals(self, symbol: str, market: str) -> Optional[dict]:
        """Try to get fundamentals from the first available adapter."""
        priority = self.get_priority(market)
        for name in priority:
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            result = adapter.fetch_fundamentals(symbol, market)
            if result:
                return result
        return None

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    def _record(self, source: str, symbol: str, status: str, bars: int):
        if source not in self._stats:
            self._stats[source] = {"ok": 0, "failed": 0, "bars": 0, "symbols": {}}
        s = self._stats[source]
        s[status] = s.get(status, 0) + 1
        s["bars"] += bars
        s["symbols"][symbol] = status

    def health_summary(self) -> dict[str, Any]:
        """Return a health dashboard suitable for _fetch_log.json."""
        result = {}
        for name, s in sorted(self._stats.items()):
            total = s["ok"] + s["failed"]
            rate = f"{s['ok'] / total * 100:.0f}%" if total > 0 else "N/A"
            result[name] = {
                "success_rate": rate,
                "ok": s["ok"],
                "failed": s["failed"],
                "bars_fetched": s["bars"],
            }
        return result

    def get_adapter(self, name: str) -> Optional[BaseAdapter]:
        return self._adapters.get(name)

    def _load_sources_config(self) -> dict:
        path = CONFIG_DIR / "sources.json"
        if not path.exists():
            return {"priority": ["yfinance", "stooq"]}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"priority": ["yfinance", "stooq"]}

    def reset_stats(self):
        self._stats.clear()

"""SourceManager — intelligent multi-source data fetching with health tracking.

Routes each symbol through a priority-ordered chain of adapters, falling back
when the primary source fails.  Tracks per-source health statistics so the
briefing engine can report data quality.
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from adapters.base import BaseAdapter
from adapters.yfinance_adapter import YFinanceAdapter
from adapters.stooq_adapter import StooqAdapter
from adapters.efinance_adapter import EFinanceAdapter
from adapters.yahoo_chart_adapter import YahooChartAdapter
from adapters.finnhub_adapter import FinnhubAdapter
from adapters.twelvedata_adapter import TwelveDataAdapter
from adapters.aktools_adapter import AkToolsAdapter
from adapters.tencent_adapter import TencentAdapter
from adapters.mootdx_adapter import MootdxAdapter
from utils.constants import TZ_BEIJING, LOG_TRUNCATE_LENGTH
from utils.exceptions import RateLimitError, GeoBlockError, AdapterNotAvailableError, MarketDataError

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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

    def _resolve_key(self) -> str:
        """Get Alpha Vantage API key via key_loader (env > local > platform)."""
        from key_loader import get_key
        return get_key("alpha_vantage_api_key", "")

    def _is_available(self) -> bool:
        return bool(self._resolve_key())

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        api_key = self._resolve_key()
        if not api_key:
            return None

        import urllib.request
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
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning("alpha_vantage(%s): request error — %s", symbol, str(e)[:LOG_TRUNCATE_LENGTH])
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
            # ── Stale data detection ──
            # AV free tier sometimes returns data months/years old.
            # Flag stale data so downstream consumers can treat it differently.
            latest_date_str = result[-1].get("date", "")
            try:
                from datetime import date as _date
                latest_date = _date.fromisoformat(latest_date_str)
                today = _date.today()
                days_old = (today - latest_date).days
                if days_old > 3:
                    logger.warning("alpha_vantage(%s): STALE — latest data is %s (%d days old)",
                                   symbol, latest_date_str, days_old)
                    for bar in result:
                        bar["stale"] = True
            except (ValueError, TypeError):
                pass

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

    Cooldown state is persisted to ``data/_cooldown.json`` so that rate-limit
    backoff survives script restarts.
    """

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}
        self._stats: dict[str, dict[str, Any]] = {}   # per-source health
        self._cooldown_path = DATA_DIR / "_cooldown.json"
        self._cooldown_dirty = False
        self._register_defaults()      # register first — _load_cooldowns needs adapters
        self._load_cooldowns()

    def _register_defaults(self):
        """Register all built-in adapters."""
        self.register(YFinanceAdapter())
        self.register(YahooChartAdapter())
        self.register(FinnhubAdapter())
        self.register(TwelveDataAdapter())
        self.register(StooqAdapter())
        self.register(EFinanceAdapter())
        self.register(AkToolsAdapter())
        self.register(TencentAdapter())
        self.register(MootdxAdapter())
        # Alpha Vantage is registered but will no-op until API key is set
        self.register(AlphaVantageAdapter())

    def register(self, adapter: BaseAdapter):
        self._adapters[adapter.name] = adapter

    # ── Cooldown persistence ──────────────────────────────────────────

    def _load_cooldowns(self):
        """Restore cooldown state from disk (survives script restart)."""
        try:
            if self._cooldown_path.exists():
                data = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
                now = _time.time()
                raw_count = len(data)
                # Auto-filter expired cooldowns
                active = {k: v for k, v in data.items() if v > now}
                expire_count = raw_count - len(active)
                if expire_count > 0:
                    logger.info("Cooldown: auto-cleaned %d expired entries", expire_count)
                for name, until_ts in active.items():
                    adapter = self._adapters.get(name)
                    if adapter:
                        adapter._cooldown_until = until_ts
                        remaining = int((until_ts - now) // 60)
                        logger.info("%s: cooldown resumed — %d min remaining", name, remaining)
                if active:
                    logger.info("Cooldown: loaded %d active entries", len(active))
        except (json.JSONDecodeError, OSError):
            logger.warning("Cooldown file corrupted — resetting")

    def _save_cooldowns(self):
        """Persist all adapter cooldowns to disk (lazy — called after batch)."""
        if not self._cooldown_dirty:
            return
        try:
            self._cooldown_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for name, adapter in self._adapters.items():
                if adapter.is_rate_limited():
                    data[name] = getattr(adapter, "_cooldown_until", 0)
            if data:
                self._cooldown_path.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8")
            elif self._cooldown_path.exists():
                self._cooldown_path.unlink()
            self._cooldown_dirty = False
        except Exception as e:
            logger.debug("Failed to save cooldown state: %s", e)

    # ── Priority resolution ───────────────────────────────────────────

    def get_priority(self, market: str) -> list[str]:
        """Read priority order from config/sources.json, filtered to market.

        If ``market_overrides`` has a market-specific list, that takes precedence
        over the global ``priority`` list (e.g. A-shares use efinance first).
        Each candidate is then filtered to adapters that actually support the market.
        """
        cfg = self._load_sources_config()
        priority = (
            cfg.get("market_overrides", {}).get(market)
            or cfg.get("priority", ["yfinance", "stooq"])
        )
        # Filter to adapters that support this market AND are enabled
        adapters_cfg = cfg.get("adapters", {})
        return [
            name for name in priority
            if name in self._adapters
            and self._adapters[name].supports_market(market)
            and adapters_cfg.get(name, {}).get("enabled", True)
        ]

    # ── Core fetch with fallback ──────────────────────────────────────

    def fetch_with_fallback(
        self,
        symbol: str,
        market: str,
        days: int = 120,
    ) -> Optional[list[dict]]:
        """Fetch OHLCV data, trying adapters in priority order.

        Skips adapters in cooldown.  On RateLimitError, persists cooldown
        and falls through to the next adapter.

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

            # Skip adapters currently in cooldown (rate-limited)
            if adapter.is_rate_limited():
                remaining = int(getattr(adapter, "_cooldown_until", 0) - _time.time())
                logger.debug("%s: skipped — cooldown %ds remaining", name, max(0, remaining))
                continue

            try:
                kline = adapter.fetch_kline(symbol, market, days)
            except RateLimitError:
                # Adapter signalled rate-limit — persist cooldown, fall through
                self._cooldown_dirty = True
                self._save_cooldowns()
                self._record(name, symbol, "rate_limited", 0)
                continue
            except GeoBlockError as e:
                # Geo-restricted source refused this IP — not a transient failure.
                self._record(name, symbol, "geo_blocked", 0)
                logger.warning("%s(%s): geo-blocked — %s",
                               name, symbol, str(e)[:LOG_TRUNCATE_LENGTH])
                continue
            except AdapterNotAvailableError as e:
                # Missing dependency (e.g. akshare not installed) — distinct from
                # a runtime failure, so operators see "install akshare" not "retry".
                self._record(name, symbol, "unavailable", 0)
                logger.warning("%s(%s): unavailable — %s",
                               name, symbol, str(e)[:LOG_TRUNCATE_LENGTH])
                continue
            except Exception as e:
                logger.warning("%s(%s): unexpected error — %s",
                           name, symbol, str(e)[:LOG_TRUNCATE_LENGTH])
                self._record(name, symbol, "failed", 0)
                continue

            if kline and len(kline) > 0:
                # Detect stale data (adapter sets "stale": True on each bar)
                is_stale = any(e.get("stale") for e in kline if isinstance(e, dict))
                status = "stale" if is_stale else "ok"
                self._record(name, symbol, status, len(kline))
                # Flush cooldowns after successful fetch (batch write)
                self._save_cooldowns()
                return kline
            else:
                self._record(name, symbol, "failed", 0)

        self._save_cooldowns()
        return None

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        """Try to get a real-time quote from the first available adapter."""
        priority = self.get_priority(market)
        for name in priority:
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            try:
                result = adapter.fetch_realtime(symbol, market)
            except MarketDataError:
                continue
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
            try:
                result = adapter.fetch_fundamentals(symbol, market)
            except MarketDataError:
                continue
            if result:
                return result
        return None

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    def _record(self, source: str, symbol: str, status: str, bars: int):
        if source not in self._stats:
            self._stats[source] = {"ok": 0, "failed": 0, "stale": 0, "bars": 0, "symbols": {}}
        s = self._stats[source]
        s[status] = s.get(status, 0) + 1
        s["bars"] += bars
        s["symbols"][symbol] = status

    def health_summary(self) -> dict[str, Any]:
        """Return a health dashboard suitable for _fetch_log.json."""
        result = {}
        for name, s in sorted(self._stats.items()):
            failed = s["failed"]
            geo = s.get("geo_blocked", 0)
            unavail = s.get("unavailable", 0)
            # geo_blocked/unavailable count against the success-rate denominator,
            # so a geo-blocked adapter reads 0% rather than a misleading "N/A".
            total = s["ok"] + failed + geo + unavail + s.get("stale", 0)
            rate = f"{s['ok'] / total * 100:.0f}%" if total > 0 else "N/A"
            result[name] = {
                "success_rate": rate,
                "ok": s["ok"],
                "failed": failed,
                "geo_blocked": geo,
                "unavailable": unavail,
                "stale": s.get("stale", 0),
                "bars_fetched": s["bars"],
            }
        return result

    def per_symbol_status(self, symbol: str) -> dict[str, str]:
        """Return {source: last_status} for one symbol across all tried sources.

        Used by fetch.py to enrich "all sources failed" error messages with the
        per-source reason (rate_limited vs geo_blocked vs unavailable vs failed).
        """
        return {
            name: s["symbols"].get(symbol, "")
            for name, s in self._stats.items()
            if symbol in s.get("symbols", {})
        }

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

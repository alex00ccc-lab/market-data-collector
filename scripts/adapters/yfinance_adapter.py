"""yfinance adapter — free Yahoo Finance data (US, JP, HK stocks + macro)."""

from __future__ import annotations

import logging
import os
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, LOG_TRUNCATE_LENGTH
from utils.exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Rate limiter shared across all yfinance calls
import time as _time
_last_yf_call = 0.0
# CI (GitHub Actions) gets looser interval; local gets tighter (already rate-limited)
_MIN_INTERVAL = 2.0 if os.getenv("CI") or os.getenv("GITHUB_ACTIONS") else 4.0


def _rate_limit():
    global _last_yf_call
    elapsed = _time.time() - _last_yf_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_yf_call = _time.time()


class YFinanceAdapter(BaseAdapter):
    """Fetches OHLCV, real-time quotes, and fundamentals via yfinance."""

    @property
    def name(self) -> str:
        return "yfinance"

    def supports_market(self, market: str) -> bool:
        return market in ("US", "JP", "HK", "EU")

    def _import(self):
        try:
            import yfinance as yf
            return yf
        except ImportError:
            return None

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        yf = self._import()
        if yf is None:
            logger.warning("yfinance not installed")
            return None

        yf_sym = symbol
        if market == "JP" and not symbol.endswith(".T"):
            yf_sym = f"{symbol}.T"
        elif market == "HK" and not symbol.endswith(".HK"):
            yf_sym = f"{symbol}.HK"
        elif market == "A" and not symbol.endswith((".SZ", ".SH")):
            yf_sym = f"{symbol}.SS"

        # Map period from days
        if days <= 5:
            period = "5d"
        elif days <= 30:
            period = "1mo"
        else:
            period = "3mo"

        # ── Attempt 1: primary ticker with full period ──
        # Skip if this adapter is in cooldown (set by SourceManager after RateLimitError)
        if self.is_rate_limited():
            logger.debug("yfinance(%s): skipped — in cooldown", symbol)
            return None

        _rate_limit()
        df = None
        had_error = False
        rate_limited = False
        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(period=period, auto_adjust=True)
        except Exception as e:
            had_error = True
            err_str = str(e)
            if "Rate" in err_str and "Limit" in err_str:
                rate_limited = True
                self.set_cooldown(900)  # 15 min — SourceManager persists this
                logger.warning("yfinance(%s): rate-limited — cooling down 15 min", symbol)
            logger.debug("yfinance(%s): %s", symbol, err_str[:LOG_TRUNCATE_LENGTH])

        # If rate-limited on first try, don't attempt second call
        if rate_limited:
            raise RateLimitError(f"yfinance rate-limited on {symbol}")

        # ── Attempt 2: shorter period fallback (at most 1 retry) ──
        if (df is None or df.empty) and period != "5d":
            _rate_limit()
            try:
                df = ticker.history(period="5d", auto_adjust=True)
            except Exception as e:
                err_str = str(e)
                if "Rate" in err_str and "Limit" in err_str:
                    self.set_cooldown(900)
                    logger.warning("yfinance(%s): rate-limited on retry", symbol)
                    raise RateLimitError(f"yfinance rate-limited on {symbol} retry")
                logger.debug("yfinance(%s): retry error — %s", symbol, err_str[:LOG_TRUNCATE_LENGTH])

        if df is None or df.empty:
            if had_error:
                logger.debug("yfinance(%s): request failed", symbol)
            else:
                logger.debug("yfinance(%s): no data (market closed or unknown ticker)", symbol)
            return None

        result = []
        for idx, row in df.iterrows():
            result.append({
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10],
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
                "source": "yfinance",
            })
        return result

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        yf = self._import()
        if yf is None:
            return None

        yf_sym = symbol
        if market == "JP" and not symbol.endswith(".T"):
            yf_sym = f"{symbol}.T"

        _rate_limit()
        try:
            ticker = yf.Ticker(yf_sym)
            info = ticker.fast_info if hasattr(ticker, "fast_info") else ticker.info
            price = (
                getattr(info, "last_price", 0)
                or getattr(info, "regular_market_price", 0)
                or 0
            )
            if price <= 0:
                return None
            now = datetime.now(TZ_BEIJING)
            return {
                "symbol": symbol.upper(),
                "price": price,
                "previous_close": getattr(info, "previous_close", 0) or getattr(info, "regular_market_previous_close", 0) or 0,
                "open": getattr(info, "open", 0) or getattr(info, "regular_market_open", 0) or 0,
                "day_high": getattr(info, "day_high", 0) or getattr(info, "regular_market_day_high", 0) or 0,
                "day_low": getattr(info, "day_low", 0) or getattr(info, "regular_market_day_low", 0) or 0,
                "volume": getattr(info, "last_volume", 0) or getattr(info, "regular_market_volume", 0) or 0,
                "source": "yfinance",
                "timestamp": now.isoformat(),
                "trade_date": now.strftime("%Y-%m-%d"),
            }
        except Exception as e:
            logger.warning("yfinance realtime(%s): %s", symbol, str(e)[:80])
            return None

    def fetch_fundamentals(self, symbol: str, market: str) -> Optional[dict]:
        yf = self._import()
        if yf is None:
            return None

        yf_sym = symbol
        if market == "JP" and not symbol.endswith(".T"):
            yf_sym = f"{symbol}.T"

        _rate_limit()
        try:
            ticker = yf.Ticker(yf_sym)
            info = ticker.info or {}
            now = datetime.now(TZ_BEIJING)
            mc = info.get("marketCap", 0) or 0
            market_cap = ""
            if mc > 1e12:
                market_cap = f"${mc/1e12:.1f}T"
            elif mc > 1e9:
                market_cap = f"${mc/1e9:.0f}B"

            # yfinance `dividendYield` 返回百分比（KO=2.39），统一 /100 为 fraction（0.0239）
            dy = info.get("dividendYield")
            dividend_yield = round(float(dy) / 100.0, 4) if isinstance(dy, (int, float)) and dy >= 0 else None

            return {
                "symbol": symbol.upper(),
                "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
                "pb_ratio": info.get("priceToBook"),
                "dividend_yield": dividend_yield,
                "market_cap": market_cap,
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "source": "yfinance",
                "timestamp": now.isoformat(),
            }
        except Exception as e:
            logger.warning("yfinance fundamentals(%s): %s", symbol, str(e)[:80])
            return None

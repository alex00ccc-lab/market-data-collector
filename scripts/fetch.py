"""Market data fetcher — standalone, no local personal_agent dependency.

Supports:
  - A/HK stocks via efinance (free, no API key)
  - US stocks & macro indices via yfinance (free, no API key)
  - Sector fund flow via efinance

All data saved as JSON under data/{date}/ for later consumption by local agent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from utils import TradingCalendar, retry, RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

# Beijing timezone
TZ_BEIJING = timezone(timedelta(hours=8))

# Market close times (Beijing time)
MARKET_CLOSE = {
    "A": 15,   # 15:00
    "HK": 16,  # 16:00
    "US": 5,   # 05:00 next day (16:00 EST)
    "JP": 14,  # 14:00 (15:00 JST)
}

# efinance market code mapping
EM_MARKET = {"A": "1", "HK": "116"}
# yfinance symbol suffix
YF_SUFFIX = {"A": ".SS", "HK": ".HK", "US": "", "JP": ".T"}
# Symbol mapping for yfinance compatibility
YF_SYMBOL_MAP = {
    "09992.HK": "9992.HK",
    "09992HK": "9992.HK",
    "00189.HK": "0189.HK",
    "160644": None,  # Fund ETF, yfinance doesn't support — skip yfinance fallback
}
# Currency per market
MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD", "JP": "JPY"}

_rate_limiter = RateLimiter(min_interval=0.5)  # min 500ms between API calls


# ============================================================================
# Configuration loading
# ============================================================================

def load_config(name: str) -> dict:
    """Load a JSON config file."""
    path = CONFIG_DIR / f"{name}.json"
    if not path.exists():
        logger.warning("Config %s not found, using empty defaults", path)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_symbols(name: str) -> list[dict]:
    """Load holdings or watchlist symbols."""
    cfg = load_config(name)
    return cfg.get("symbols", [])


# ============================================================================
# efinance (A/HK) fetchers
# ============================================================================

def _efinance_secid(symbol: str, market: str) -> str:
    """Build eastmoney secid from symbol.

    Examples:
      002008.SZ → 0.002008 (Shenzhen)
      600519.SH → 1.600519 (Shanghai)
      0189HK → 116.00189
    """
    import urllib.request
    code = symbol.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")
    if market == "A":
        if code.startswith(("0", "3")):
            return f"0.{code}"  # Shenzhen
        return f"1.{code}"  # Shanghai
    if market == "HK":
        # Remove leading zeros for efinance HK
        return f"116.{code}"
    return f"1.{code}"


def _efinance_http(url: str, timeout: int = 15) -> Optional[dict]:
    """Simple HTTP GET to efinance API."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("efinance HTTP error: %s", str(e)[:80])
        return None


@retry(max_attempts=2, delay=1.0)
def fetch_efinance_kline(symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
    """Fetch daily K-line from efinance (A/HK)."""
    secid = _efinance_secid(symbol, market)

    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&end=20500101&lmt={days}"
    )

    _rate_limiter.wait()
    data = _efinance_http(url)
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
            "adj": "qfq",
            "source": "efinance",
        })
    return result


@retry(max_attempts=2, delay=1.0)
def fetch_efinance_realtime(symbol: str, market: str) -> Optional[dict]:
    """Fetch real-time quote from efinance."""
    secid = _efinance_secid(symbol, market)

    url = (
        f"https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}"
        f"&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f170"
    )

    _rate_limiter.wait()
    data = _efinance_http(url)
    if not data or "data" not in data or not data["data"]:
        return None

    d = data["data"]
    now = datetime.now(TZ_BEIJING)

    price = d.get("f43", 0) / 100 if d.get("f43") else 0
    if price <= 0:
        return None

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
        "adj": "normal",
        "source": "efinance",
        "timestamp": now.isoformat(),
        "trade_date": now.strftime("%Y-%m-%d"),
    }


@retry(max_attempts=2, delay=1.0)
def fetch_sector_flow() -> Optional[list[dict]]:
    """Fetch sector fund flow rankings."""
    url = (
        "https://push2.eastmoney.com/api/qt/clt/get"
        "?fields=f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87"
        "&fid=f62&po=1&pz=20&np=1&fltt=2&invt=2"
    )

    _rate_limiter.wait()
    data = _efinance_http(url)
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


# ============================================================================
# yfinance (US/macro) fetchers
# ============================================================================

def _check_yfinance() -> bool:
    try:
        import yfinance as yf  # noqa: F401
        return True
    except ImportError:
        logger.warning("yfinance not installed — US/HK data unavailable")
        return False


@retry(max_attempts=2, delay=2.0)
def fetch_yfinance_history(symbol: str, period: str = "3mo") -> Optional[list[dict]]:
    """Fetch OHLCV from yfinance.

    If primary period fails, automatically retries with '5d' short window
    before giving up.  Detailed error logging helps diagnose root causes.
    """
    if not _check_yfinance():
        return None

    import yfinance as yf

    _rate_limiter.wait()
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, auto_adjust=True)
        if df.empty:
            # Try shorter window as fallback
            if period != "5d":
                logger.debug("yfinance(%s): empty for %s, retrying with 5d window", symbol, period)
                _rate_limiter.wait()
                df = ticker.history(period="5d", auto_adjust=True)
                if df.empty:
                    logger.debug("yfinance(%s): empty for 5d too", symbol)
                    return None
            else:
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
                "adj": "qfq",
                "source": "yfinance",
            })
        return result
    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)[:120]
        # Classify error for easier diagnosis
        if "Connection" in err_type or "RemoteDisconnected" in err_type:
            logger.warning("yfinance(%s): NETWORK error — %s: %s", symbol, err_type, err_msg)
        elif "Timeout" in err_type or "timed out" in err_msg.lower():
            logger.warning("yfinance(%s): TIMEOUT — %s: %s", symbol, err_type, err_msg)
        elif "Rate" in err_msg or "Too Many" in err_msg:
            logger.warning("yfinance(%s): RATE LIMITED — %s: %s", symbol, err_type, err_msg)
        else:
            logger.warning("yfinance(%s): %s — %s", symbol, err_type, err_msg)
        return None


@retry(max_attempts=2, delay=2.0)
def fetch_yfinance_realtime(symbol: str) -> Optional[dict]:
    """Fetch real-time quote from yfinance."""
    if not _check_yfinance():
        return None

    import yfinance as yf

    _rate_limiter.wait()
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info if hasattr(ticker, "fast_info") else ticker.info

        now = datetime.now(TZ_BEIJING)
        price = (
            getattr(info, "last_price", 0)
            or getattr(info, "regular_market_price", 0)
            or 0
        )
        if price <= 0:
            return None

        return {
            "symbol": symbol.upper(),
            "price": price,
            "previous_close": getattr(info, "previous_close", 0) or getattr(info, "regular_market_previous_close", 0) or 0,
            "open": getattr(info, "open", 0) or getattr(info, "regular_market_open", 0) or 0,
            "day_high": getattr(info, "day_high", 0) or getattr(info, "regular_market_day_high", 0) or 0,
            "day_low": getattr(info, "day_low", 0) or getattr(info, "regular_market_day_low", 0) or 0,
            "volume": getattr(info, "last_volume", 0) or getattr(info, "regular_market_volume", 0) or 0,
            "adj": "normal",
            "source": "yfinance",
            "timestamp": now.isoformat(),
            "trade_date": now.strftime("%Y-%m-%d"),
        }
    except Exception as e:
        logger.warning("yfinance realtime(%s) error: %s", symbol, str(e)[:80])
        return None


@retry(max_attempts=2, delay=2.0)
def fetch_yfinance_fundamentals(symbol: str) -> Optional[dict]:
    """Fetch PE, PB, dividend yield, market cap."""
    if not _check_yfinance():
        return None

    import yfinance as yf

    _rate_limiter.wait()
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        now = datetime.now(TZ_BEIJING)
        return {
            "symbol": symbol.upper(),
            "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "dividend_yield": info.get("dividendYield"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "source": "yfinance",
            "timestamp": now.isoformat(),
            "trade_date": now.strftime("%Y-%m-%d"),
        }
    except Exception as e:
        logger.warning("yfinance fundamentals(%s) error: %s", symbol, str(e)[:80])
        return None


# ============================================================================
# Stooq adapter (simple CSV downloader) — free, no API key
# ============================================================================

@retry(max_attempts=2, delay=1.0)
def fetch_stooq_history(symbol: str, market: str = "US") -> Optional[list[dict]]:
    """Fetch daily OHLCV from Stooq CSV endpoint.

    Stooq uses lowercase symbols with market-specific suffixes:
      - US: {ticker}.us  (e.g. tsla.us)
      - JP: {ticker}.jp  (e.g. 6981.jp)
      - HK: {ticker}.hk  (e.g. 09992.hk)
    Falls back to bare ticker and tries multiple variants.
    """
    import urllib.request

    # Build candidate stooq symbol forms to try
    cand = []
    s = symbol.strip()
    suffix_map = {"US": ".us", "JP": ".jp", "HK": ".hk", "A": ".sh"}

    if "." in s:
        base, suf = s.split(".", 1)
        cand.append(f"{base.lower()}.{suf.lower()}")
        cand.append(base.lower())
        # Also try market suffix
        msuf = suffix_map.get(market, ".us")
        cand.append(f"{base.lower()}{msuf}")
    else:
        # No suffix — try market-appropriate forms
        msuf = suffix_map.get(market, ".us")
        cand.append(f"{s.lower()}{msuf}")
        cand.append(s.lower())
        # For US tickers, also try without suffix (some work bare)
        if market == "US":
            cand.append(f"{s.lower()}.usd")

    headers = {"User-Agent": "Mozilla/5.0"}

    for c in cand:
        url = f"https://stooq.com/q/d/l/?s={c}&i=d"
        _rate_limiter.wait()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8-sig")
        except Exception as e:
            logger.debug("stooq(%s) request failed: %s", c, str(e)[:80])
            continue

        # Parse CSV
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines or lines[0].lower().startswith("no data"):
            logger.debug("stooq(%s): no data returned", c)
            continue
        # Expect header: Date,Open,High,Low,Close,Volume
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
                vol = int(float(parts[5])) if parts[5] not in ("","-") else 0
            except Exception:
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
            logger.info("stooq(%s) OK — %d rows via candidate '%s'", symbol, len(rows), c)
            return rows

    logger.debug("stooq(%s): all %d candidates failed", symbol, len(cand))
    return None


# ============================================================================
# Main orchestration
# ============================================================================

def fetch_all(today: Optional[date] = None, force: bool = False) -> dict[str, Any]:
    """Fetch all data for today. Returns summary dict.

    Args:
        today: Target date (default: today Beijing time).
        force: Force fetch even if the calendar suggests skipping.

    Returns:
        {"quotes": {symbol: path}, "macro": path, "sectors": path, "errors": [...]}
    """
    if today is None:
        today = datetime.now(TZ_BEIJING).date()

    date_str = today.strftime("%Y-%m-%d")
    quotes_dir = DATA_DIR / date_str / "quotes"
    macro_dir = DATA_DIR / date_str
    quotes_dir.mkdir(parents=True, exist_ok=True)

    cal = TradingCalendar()
    errors: list[str] = []
    fetched: dict[str, str] = {}
    skipped: list[str] = []
    per_symbol: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 1. Holdings symbols — OHLCV
    # ------------------------------------------------------------------
    holdings = load_symbols("holdings")
    logger.info("Fetching %d holdings symbols...", len(holdings))

    for item in holdings:
        sym = item["symbol"]
        market = item.get("market", "A")

        # Skip if market closed and no real-time needed for daily fetch
        if not force and not cal.should_fetch(market, today):
            skipped.append(f"{sym} ({market}: market closed or not in fetch window)")
            continue

        # Fetch kline — efinance first, yfinance fallback for A/HK
        kline = None
        if market in ("A", "HK"):
            kline = fetch_efinance_kline(sym, market)
            # Fallback: yfinance for HK stocks (efinance may block non-CN IPs)
            if not kline and market == "HK":
                # Use symbol map or auto-convert
                yf_sym = YF_SYMBOL_MAP.get(sym, sym.replace("HK", ".HK"))
                if yf_sym:  # None means "skip yfinance for this symbol"
                    kline = fetch_yfinance_history(yf_sym, period="3mo")
                    if kline:
                        logger.info("  %s: yfinance fallback OK (%d bars via %s)", sym, len(kline), yf_sym)
            # Fallback: yfinance for A-shares
            if not kline and market == "A":
                yf_sym = YF_SYMBOL_MAP.get(sym, sym)
                if yf_sym:  # None means skip
                    kline = fetch_yfinance_history(yf_sym, period="3mo")
                    if kline:
                        logger.info("  %s: yfinance fallback OK (%d bars)", sym, len(kline))
        elif market == "US":
            kline = fetch_yfinance_history(sym, period="3mo")
            # realtime fallback for US if history empty
            if not kline:
                realtime = fetch_yfinance_realtime(sym)
                if realtime:
                    now_ts = datetime.now(TZ_BEIJING).isoformat()
                    kline = [{
                        "date": realtime.get("trade_date", now_ts[:10]),
                        "close": realtime.get("price"),
                        "open": realtime.get("open", 0),
                        "high": realtime.get("day_high", 0),
                        "low": realtime.get("day_low", 0),
                        "volume": realtime.get("volume", 0),
                        "adj": realtime.get("adj", "normal"),
                        "source": "realtime-fallback",
                        "timestamp": realtime.get("timestamp", now_ts),
                    }]
                    logger.info("  %s: realtime fallback OK (price=%s)", sym, kline[0]["close"])
        elif market == "JP":
            # Japanese stocks via yfinance with .T suffix
            yf_sym = sym if sym.endswith(".T") else f"{sym}.T"
            kline = fetch_yfinance_history(yf_sym, period="3mo")
            if kline:
                logger.info("  %s: yfinance OK (%d bars via %s)", sym, len(kline), yf_sym)

        if kline:
            out_path = quotes_dir / f"{sym}.json"
            fetched_at = datetime.now(TZ_BEIJING).isoformat()
            quote_date = None
            try:
                for e in kline:
                    if isinstance(e, dict):
                        if "source" not in e:
                            e["source"] = e.get("source", "yfinance")
                        if "timestamp" not in e:
                            e["timestamp"] = fetched_at
                        if quote_date is None and e.get("date"):
                            quote_date = e.get("date")
            except Exception:
                pass

            out_path.write_text(
                json.dumps(kline, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            fetched[sym] = str(out_path)
            source = kline[0].get("source") if isinstance(kline, list) and kline and isinstance(kline[0], dict) else "unknown"
            per_symbol[sym] = {
                "status": "ok",
                "source": source,
                "fetched_at": fetched_at,
                "quote_date": quote_date or date_str,
                "path": str(out_path),
            }
            logger.info("  %s: %d bars saved (source=%s)", sym, len(kline), source)
        else:
            # Attempt configured fallback sources (e.g., Stooq) if yfinance failed
            cfg = load_config("sources")
            priority = cfg.get("priority", []) if isinstance(cfg, dict) else []
            tried_alt = False
            if "stooq" in priority:
                try:
                    # prefer yf_sym when available in this scope
                    yf_try = locals().get('yf_sym') or sym
                    stooq_k = fetch_stooq_history(yf_try, market=market)
                    tried_alt = True
                    if stooq_k:
                        kline = stooq_k
                        logger.info("  %s: stooq fallback OK (%d bars)", sym, len(kline))
                except Exception:
                    logger.debug("stooq fallback failed for %s", sym)

            if kline:
                out_path = quotes_dir / f"{sym}.json"
                fetched_at = datetime.now(TZ_BEIJING).isoformat()
                quote_date = None
                try:
                    for e in kline:
                        if isinstance(e, dict):
                            if "source" not in e:
                                e["source"] = e.get("source", "stooq")
                            if "timestamp" not in e:
                                e["timestamp"] = fetched_at
                            if quote_date is None and e.get("date"):
                                quote_date = e.get("date")
                except Exception:
                    pass

                out_path.write_text(
                    json.dumps(kline, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                fetched[sym] = str(out_path)
                per_symbol[sym] = {
                    "status": "ok",
                    "source": kline[0].get("source") if isinstance(kline, list) and kline and isinstance(kline[0], dict) else "stooq",
                    "fetched_at": fetched_at,
                    "quote_date": quote_date or date_str,
                    "path": str(out_path),
                }
                logger.info("  %s: %d bars saved (source=%s)", sym, len(kline), per_symbol[sym]["source"])
            else:
                sources_tried = ", ".join(priority) if priority else "yfinance"
                if tried_alt:
                    err_msg = f"{sym}: all sources failed (tried: {sources_tried})"
                else:
                    err_msg = f"{sym}: kline fetch failed (tried: yfinance)"
                errors.append(err_msg)
                per_symbol[sym] = {"status": "failed", "error": err_msg, "sources_tried": sources_tried}
                logger.warning("  %s: FAILED (tried: %s)", sym, sources_tried)

    # Also fetch watchlist symbols
    watchlist = load_symbols("watchlist")
    if watchlist:
        logger.info("Fetching %d watchlist symbols...", len(watchlist))
        for item in watchlist:
            sym = item["symbol"]
            market = item.get("market", "A")

            if market in ("A", "HK"):
                kline = fetch_efinance_kline(sym, market)
                if not kline and market == "HK":
                    yf_sym = YF_SYMBOL_MAP.get(sym, sym.replace("HK", ".HK"))
                    if yf_sym:
                        kline = fetch_yfinance_history(yf_sym, period="3mo")
                if not kline and market == "A":
                    yf_sym = YF_SYMBOL_MAP.get(sym, sym)
                    if yf_sym:
                        kline = fetch_yfinance_history(yf_sym, period="3mo")
            elif market == "US":
                kline = fetch_yfinance_history(sym, period="3mo")
            elif market == "JP":
                yf_sym = sym if sym.endswith(".T") else f"{sym}.T"
                kline = fetch_yfinance_history(yf_sym, period="3mo")

            if kline:
                out_path = quotes_dir / f"{sym}.json"
                fetched_at = datetime.now(TZ_BEIJING).isoformat()
                try:
                    for e in kline:
                        if isinstance(e, dict):
                            if "source" not in e:
                                e["source"] = e.get("source", "yfinance")
                            if "timestamp" not in e:
                                e["timestamp"] = fetched_at
                except Exception:
                    pass
                out_path.write_text(
                    json.dumps(kline, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                quote_date = None
                try:
                    for e in kline:
                        if isinstance(e, dict) and quote_date is None:
                            quote_date = e.get("date")
                except Exception:
                    pass
                fetched[sym] = str(out_path)
                per_symbol[sym] = {
                    "status": "ok",
                    "source": kline[0].get("source") if isinstance(kline, list) and kline and isinstance(kline[0], dict) else "yfinance",
                    "fetched_at": fetched_at,
                    "quote_date": quote_date or date_str,
                    "path": str(out_path),
                }
            else:
                err_msg = f"{sym} (watchlist): kline fetch failed"
                errors.append(err_msg)
                per_symbol[sym] = {"status": "failed", "error": err_msg}

    # ------------------------------------------------------------------
    # 2. Macro indicators
    # ------------------------------------------------------------------
    macro_cfg = load_config("macro")
    macro_indicators = macro_cfg.get("indicators", [])
    macro_results: dict[str, Any] = {}

    logger.info("Fetching %d macro indicators...", len(macro_indicators))
    for ind in macro_indicators:
        sym = ind["symbol"]
        src = ind.get("source", "yfinance")
        mkt = ind.get("market", "US")

        if src == "efinance":
            kline = fetch_efinance_kline(sym, mkt, days=30)
            if kline and len(kline) > 0:
                latest = kline[-1]
                macro_results[sym] = {
                    "name": ind["name"],
                    "price": latest["close"],
                    "date": latest["date"],
                    "change_pct": None,
                }
        else:
            kline = fetch_yfinance_history(sym, period="1mo")
            if kline and len(kline) > 0:
                latest = kline[-1]
                prev = kline[-2] if len(kline) >= 2 else latest
                chg = ((latest["close"] - prev["close"]) / prev["close"] * 100) if prev["close"] > 0 else 0
                macro_results[sym] = {
                    "name": ind["name"],
                    "price": round(latest["close"], 2),
                    "date": latest["date"],
                    "change_pct": round(chg, 2),
                }

    if macro_results:
        macro_path = macro_dir / "macro.json"
        macro_path.write_text(
            json.dumps(macro_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        fetched["_macro"] = str(macro_path)

    # ------------------------------------------------------------------
    # 3. Sector flow
    # ------------------------------------------------------------------
    logger.info("Fetching sector fund flow...")
    sectors = fetch_sector_flow()
    if sectors:
        sector_path = macro_dir / "sectors.json"
        sector_path.write_text(
            json.dumps(sectors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        fetched["_sectors"] = str(sector_path)
    else:
        logger.warning("sector_flow: fetch failed (non-critical, continuing)")

    # ------------------------------------------------------------------
    # Write _fetch_log.json
    # ------------------------------------------------------------------
    n_success = len([k for k in fetched if not k.startswith("_")])
    log_data = {
        "run_at": datetime.now(TZ_BEIJING).isoformat(),
        "date": date_str,
        "symbols_attempted": len(holdings) + len(watchlist),
        "symbols_succeeded": n_success,
        "symbols_failed": [e.split(":")[0].strip() for e in errors],
        "errors": errors,
        "skipped": skipped,
        "per_symbol": per_symbol,
    }
    log_path = macro_dir / "_fetch_log.json"
    log_path.write_text(json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write fallback skeletons for failed symbols (prevents Obsidian render errors)
    for item in holdings + watchlist:
        sym = item["symbol"]
        if sym not in fetched:
            skeleton_path = quotes_dir / f"{sym}.json"
            if not skeleton_path.exists():
                # Write a single-bar OHLCV skeleton so downstream indicator
                # computation can safely read `date` and short-length series.
                skeleton_bar = {
                    "symbol": sym,
                    "date": date_str,
                    "open": 0.0,
                    "high": 0.0,
                    "low": 0.0,
                    "close": 0.0,
                    "volume": 0,
                    "market": item.get("market", ""),
                    "source": "fallback",
                    "message": f"Data unavailable for {date_str}",
                    "fetched_at": datetime.now(TZ_BEIJING).isoformat(),
                }
                skeleton_path.write_text(
                    json.dumps([skeleton_bar], ensure_ascii=False),
                    encoding="utf-8",
                )
                per_symbol[sym] = {
                    "status": "failed",
                    "source": "fallback",
                    "fetched_at": skeleton_bar["fetched_at"],
                    "quote_date": date_str,
                    "path": str(skeleton_path),
                }
                logger.info("  %s: fallback skeleton written", sym)

    logger.info(
        "Fetch complete: %d/%d symbols OK, %d errors, %d skipped",
        n_success, len(holdings) + len(watchlist), len(errors), len(skipped),
    )

    return {
        "date": date_str,
        "quotes_fetched": n_success,
        "total_holdings": len(holdings),
        "total_watchlist": len(watchlist),
        "errors": errors,
        "skipped": skipped,
        "files": fetched,
    }


# ============================================================================
# CLI entry point
# ============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch market data")
    parser.add_argument("--date", type=str, default=None, help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--force", action="store_true", help="Force fetch even outside recommended window")
    parser.add_argument("--lenient", action="store_true", help="Exit 0 even if some symbols fail (for CI pipelines)")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    result = fetch_all(target, force=args.force)

    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False, indent=2))

    if result["errors"]:
        n_ok = result.get("quotes_fetched", 0)
        n_total = result.get("total_holdings", 0) + result.get("total_watchlist", 0)
        print(f"\n⚠️  {len(result['errors'])} errors ({n_ok}/{n_total} OK):", file=sys.stderr)
        for e in result["errors"]:
            print(f"  - {e}", file=sys.stderr)
        if not args.lenient:
            sys.exit(1)
        else:
            print("[lenient] Continuing despite errors (CI pipeline mode)", file=sys.stderr)

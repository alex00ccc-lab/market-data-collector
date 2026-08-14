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
# Currency → yfinance exchange suffix fallback (for non-US stocks with US market label)
CURRENCY_SUFFIX_MAP = {
    "SEK": [".ST"],   # Nasdaq Stockholm (e.g. SIVE → SIVE.ST)
    "DKK": [".CO"],   # Nasdaq Copenhagen
    "NOK": [".OL"],   # Oslo Børs
    "EUR": [".DE", ".PA", ".AS", ".MI"],  # Xetra, Euronext Paris, Amsterdam, Milan
    "CHF": [".SW"],   # SIX Swiss Exchange
    "GBP": [".L"],    # London Stock Exchange
}

_rate_limiter = RateLimiter(min_interval=1.5)  # min 1.5s between API calls to avoid rate limiting


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

def fetch_all(today: Optional[date] = None, force: bool = False,
              markets: Optional[list[str]] = None) -> dict[str, Any]:
    """Fetch all data for today. Returns summary dict.

    Args:
        today: Target date (default: today Beijing time).
        force: Force fetch even if the calendar suggests skipping.
        markets: Optional list of market codes to restrict the run to
            (e.g. ["A", "HK"]). None = all markets (backward compatible).

    Returns:
        {"quotes": {symbol: path}, "macro": path, "sectors": path, "errors": [...]}
    """
    if today is None:
        today = datetime.now(TZ_BEIJING).date()

    if markets is not None:
        markets = [m.strip().upper() for m in markets if m and m.strip()]

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
    # 1. Holdings + Watchlist symbols — OHLCV via SourceManager
    # ------------------------------------------------------------------
    from source_manager import SourceManager
    mgr = SourceManager()
    mgr.reset_stats()

    holdings = load_symbols("holdings")
    watchlist = load_symbols("watchlist")
    all_symbols = holdings + watchlist
    if markets is not None:
        all_symbols = [it for it in all_symbols if it.get("market", "A") in markets]
        logger.info("Restricting to markets [%s] — %d symbols in scope",
                    ",".join(markets), len(all_symbols))
    logger.info("Fetching %d holdings + %d watchlist symbols via multi-source pipeline...",
               len(holdings), len(watchlist))

    for item in all_symbols:
        sym = item["symbol"]
        market = item.get("market", "A")

        # Skip if market closed and not forcing
        if not force and not cal.should_fetch(market, today):
            skipped.append(f"{sym} ({market}: market closed or not in fetch window)")
            per_symbol[sym] = {
                "status": "skipped",
                "reason": f"{market} market closed on {date_str}",
                "source": None,
                "fetched_at": datetime.now(TZ_BEIJING).isoformat(),
                "quote_date": None,
            }
            continue

        # ═══ Multi-source fetch with automatic fallback ═══
        kline = mgr.fetch_with_fallback(sym, market)

        # Currency-based suffix fallback for Nordic/European stocks
        # (e.g. SIVE on Nasdaq Stockholm needs .ST suffix for yfinance)
        if not kline:
            currency = item.get("currency", "")
            suffixes = CURRENCY_SUFFIX_MAP.get(currency, [])
            base_sym = sym.split(".")[0] if "." in sym else sym
            for suffix in suffixes:
                alt_sym = f"{base_sym}{suffix}"
                if alt_sym == sym:
                    continue
                kline = mgr.fetch_with_fallback(alt_sym, market)
                if kline:
                    logger.info("  %s: resolved via currency suffix %s → %s", sym, suffix, alt_sym)
                    break

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
                        # Track the LATEST bar date (not first — was a bug)
                        if e.get("date"):
                            quote_date = e.get("date")
            except Exception:
                pass

            out_path.write_text(
                json.dumps(kline, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            fetched[sym] = str(out_path)
            source = kline[0].get("source") if isinstance(kline, list) and kline and isinstance(kline[0], dict) else "unknown"

            # Detect stale data (e.g. Alpha Vantage returning months-old data)
            is_stale = any(e.get("stale") for e in kline if isinstance(e, dict))
            status = "stale" if is_stale else "ok"

            per_symbol[sym] = {
                "status": status,
                "source": source,
                "fetched_at": fetched_at,
                "quote_date": quote_date or date_str,
                "path": str(out_path),
            }
            if is_stale:
                logger.warning("  %s: %d bars saved (source=%s, STALE — latest=%s)",
                             sym, len(kline), source, quote_date)
            else:
                logger.info("  %s: %d bars saved (source=%s)", sym, len(kline), source)
        else:
            priority = mgr.get_priority(market)
            err_msg = f"{sym}: all sources failed (tried: {', '.join(priority)})"
            errors.append(err_msg)
            per_symbol[sym] = {
                "status": "missing",
                "error": err_msg,
                "sources_tried": ", ".join(priority),
                "quote_date": None,
            }
            logger.warning("  %s: MISSING (tried: %s)", sym, ", ".join(priority))

    # ------------------------------------------------------------------
    # 2. Macro indicators (gated by trading calendar — no longer runs unconditionally)
    # ------------------------------------------------------------------
    if markets is not None and "US" not in markets:
        logger.info("Skipping macro fetch — US not in requested markets")
    elif not force and not cal.should_fetch(market="US", d=today):
        logger.info("Skipping macro fetch — not a trading day or outside fetch window")
    else:
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
                # P2 fix: route through SourceManager for real fallback. Macro
                # symbols are already Yahoo-native (^VIX, ^TNX, ^HSI…); passing
                # market="US" keeps the adapter from appending a country suffix
                # (^HSI→^HSI.HK would corrupt it) while US indices still gain the
                # finnhub/alpha_vantage fallback chain.
                kline = mgr.fetch_with_fallback(sym, "US", days=30)
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
    # 3. Sector flow (A-share only — gated by requested markets)
    # ------------------------------------------------------------------
    if markets is not None and "A" not in markets:
        logger.info("Skipping sector flow — A not in requested markets")
    else:
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
    n_ok = len([k for k in per_symbol if per_symbol[k].get("status") == "ok"])
    n_stale = len([k for k in per_symbol if per_symbol[k].get("status") == "stale"])
    n_skipped = len(skipped)
    n_real_success = n_ok  # only fresh data counts as success
    log_data = {
        "run_at": datetime.now(TZ_BEIJING).isoformat(),
        "date": date_str,
        "symbols_attempted": len(all_symbols),
        "symbols_succeeded": n_real_success,
        "symbols_stale": n_stale,
        "symbols_skipped": n_skipped,
        "symbols_failed": [e.split(":")[0].strip() for e in errors],
        "errors": errors,
        "skipped": skipped,
        "per_symbol": per_symbol,
        "source_health": mgr.health_summary(),
    }
    # Per-run log (non-clobbering): CI (US/JP/EU) and local (A/HK) write disjoint
    # market sets, so each keeps its own record instead of overwriting the other's.
    run_markets = "_".join(sorted(markets)) if markets else "all"
    (macro_dir / f"_fetch_log_{run_markets}.json").write_text(
        json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Aggregate log (backward-compatible): last writer wins for now; downstream
    # can be upgraded to read per-run files for a full cross-market view.
    log_path = macro_dir / "_fetch_log.json"
    log_path.write_text(json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # D4 (标注式缺失): no close:0 skeleton bars.  A symbol whose fetch failed
    # is recorded as "missing" in per_symbol above; its quote file is simply
    # absent so downstream consumers fall back to the last valid bar instead of
    # ingesting a 0-price bar that would corrupt MA/RSI/MACD.

    logger.info(
        "Fetch complete: %d/%d symbols OK, %d errors, %d skipped",
        n_ok, len(all_symbols), len(errors), len(skipped),
    )

    return {
        "date": date_str,
        "quotes_fetched": n_ok,
        "total_holdings": len(holdings),
        "total_watchlist": len(watchlist),
        "total_attempted": len(all_symbols),
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
    parser.add_argument("--markets", type=str, default=None,
                        help="Comma-separated market codes to restrict fetch to (e.g. A,HK or US,JP)")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    markets = [m.strip() for m in args.markets.split(",")] if args.markets else None
    result = fetch_all(target, force=args.force, markets=markets)

    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False, indent=2))

    if result["errors"]:
        n_ok = result.get("quotes_fetched", 0)
        n_total = result.get("total_attempted",
                             result.get("total_holdings", 0) + result.get("total_watchlist", 0))
        print(f"\n⚠️  {len(result['errors'])} errors ({n_ok}/{n_total} OK):", file=sys.stderr)
        for e in result["errors"]:
            print(f"  - {e}", file=sys.stderr)
        if not args.lenient:
            sys.exit(1)
        else:
            print("[lenient] Continuing despite errors (CI pipeline mode)", file=sys.stderr)

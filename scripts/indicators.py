"""Technical indicator pre-computation — standalone.

Calculates from OHLCV data:
  - MA (5, 10, 20, 60)
  - RSI-14
  - MACD (DIFF, DEA, histogram)
  - KDJ (K, D, J)
  - Bollinger Bands (upper, middle, lower, bandwidth)
  - Fibonacci retracement levels
  - Trend strength (bullish/neutral/bearish)
  - Support / Resistance levels

All pure math — zero API calls, zero LLM tokens.
Results saved alongside quote data for instant consumption by local agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("indicators")


# ============================================================================
# Data classes
# ============================================================================

@dataclass
class IndicatorResult:
    """Complete technical indicator snapshot for one symbol."""
    symbol: str
    date: str                          # Latest data date
    close: float = 0.0
    # MA
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma_alignment: str = "unknown"      # bullish / bearish / mixed
    # RSI
    rsi14: float = 50.0
    rsi_signal: str = "neutral"        # overbought / oversold / neutral / bullish / bearish
    # MACD
    macd_diff: float = 0.0
    macd_dea: float = 0.0
    macd_histogram: float = 0.0
    macd_signal: str = "neutral"       # golden_cross / death_cross / divergence
    # KDJ
    kdj_k: float = 50.0
    kdj_d: float = 50.0
    kdj_j: float = 50.0
    kdj_signal: str = "neutral"
    # Bollinger
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_bandwidth: float = 0.0          # (upper - lower) / middle * 100
    bb_position: float = 0.5           # 0=lower, 1=upper
    bb_signal: str = "neutral"
    # Volume
    vol_ratio: float = 1.0             # latest / 5-day avg
    vol_signal: str = "normal"         # surge / shrink / normal
    # Fibonacci
    fib_levels: dict[str, float] = field(default_factory=dict)
    # Trend
    trend_strength: str = "震荡"        # 多头 / 震荡 / 空头
    # Support / Resistance
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    # Resonance
    bullish_count: int = 0
    neutral_count: int = 0
    bearish_count: int = 0
    overall: str = "neutral"           # bullish / mildly_bullish / neutral / mildly_bearish / bearish

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "date": self.date,
            "close": self.close,
            "ma": {"ma5": self.ma5, "ma10": self.ma10, "ma20": self.ma20, "ma60": self.ma60,
                   "alignment": self.ma_alignment},
            "rsi": {"value": self.rsi14, "signal": self.rsi_signal},
            "macd": {"diff": self.macd_diff, "dea": self.macd_dea, "histogram": self.macd_histogram,
                     "signal": self.macd_signal},
            "kdj": {"k": self.kdj_k, "d": self.kdj_d, "j": self.kdj_j, "signal": self.kdj_signal},
            "bollinger": {"upper": self.bb_upper, "middle": self.bb_middle, "lower": self.bb_lower,
                          "bandwidth": self.bb_bandwidth, "position": self.bb_position, "signal": self.bb_signal},
            "volume": {"ratio": self.vol_ratio, "signal": self.vol_signal},
            "fibonacci": self.fib_levels,
            "trend_strength": self.trend_strength,
            "supports": self.supports,
            "resistances": self.resistances,
            "resonance": {
                "bullish": self.bullish_count,
                "neutral": self.neutral_count,
                "bearish": self.bearish_count,
                "overall": self.overall,
            },
        }


# ============================================================================
# Math utilities
# ============================================================================

def _ema(data: list[float], period: int) -> list[float]:
    """Compute EMA series."""
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []
    result = [sum(data[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in data[period:]:
        result.append((price - result[-1]) * multiplier + result[-1])
    return result


def _sma(data: list[float], period: int) -> float:
    """Simple moving average of last `period` values."""
    if len(data) < period:
        return 0.0
    return sum(data[-period:]) / period


# ============================================================================
# Individual indicator calculators
# ============================================================================

def calc_ma(closes: list[float]) -> dict:
    """Calculate MA alignment."""
    if len(closes) < 20:
        return {"ma5": 0, "ma10": 0, "ma20": 0, "ma60": 0, "alignment": "insufficient_data"}

    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60) if len(closes) >= 60 else 0

    close = closes[-1]
    aligned = ma5 >= ma10 >= ma20
    above_ma20 = close >= ma20

    if aligned and above_ma20:
        alignment = "bullish"
    elif not above_ma20:
        alignment = "bearish"
    else:
        alignment = "mixed"

    return {"ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
            "ma60": round(ma60, 2), "alignment": alignment}


def calc_rsi(closes: list[float], period: int = 14) -> dict:
    """Calculate RSI."""
    if len(closes) < period + 1:
        return {"value": 50.0, "signal": "insufficient_data"}

    gains, losses = [], []
    for i in range(-period, 0):
        chg = closes[i] - closes[i - 1]
        gains.append(chg if chg > 0 else 0)
        losses.append(abs(chg) if chg < 0 else 0)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = 100 - (100 / (1 + rs))
    rsi = round(rsi, 1)

    if rsi > 70:
        signal = "overbought"
    elif rsi < 30:
        signal = "oversold"
    elif rsi > 50:
        signal = "bullish"
    else:
        signal = "bearish"

    return {"value": rsi, "signal": signal}


def calc_macd(closes: list[float]) -> dict:
    """Calculate MACD (12, 26, 9)."""
    if len(closes) < 35:
        return {"diff": 0, "dea": 0, "histogram": 0, "signal": "insufficient_data"}

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    diffs = [e12 - e26 for e12, e26 in zip(ema12[-9:], ema26[-9:])]
    diff = round(diffs[-1], 4)
    dea = round(sum(diffs) / len(diffs), 4)
    histogram = round(2 * (diff - dea), 4)

    if diff > dea and histogram > 0:
        signal = "golden_cross"
    elif diff < dea and histogram < 0:
        signal = "death_cross"
    else:
        signal = "divergence"

    return {"diff": diff, "dea": dea, "histogram": histogram, "signal": signal}


def calc_kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> dict:
    """Calculate KDJ indicator."""
    if len(closes) < n + 1:
        return {"k": 50, "d": 50, "j": 50, "signal": "insufficient_data"}

    low_n = min(lows[-n:])
    high_n = max(highs[-n:])
    rsv = (closes[-1] - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50

    # Simplified: K starts at 50 (would need prior values for accurate calculation)
    k = rsv * 1/3 + 50 * 2/3
    d = k * 1/3 + 50 * 2/3
    j = 3 * k - 2 * d

    k, d, j = round(k, 1), round(d, 1), round(j, 1)

    if k > 80:
        signal = "overbought"
    elif k < 20:
        signal = "oversold"
    elif k > 50:
        signal = "bullish"
    else:
        signal = "bearish"

    return {"k": k, "d": d, "j": j, "signal": signal}


def calc_bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0) -> dict:
    """Calculate Bollinger Bands."""
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "bandwidth": 0, "position": 0.5, "signal": "insufficient_data"}

    ma20 = sum(closes[-period:]) / period
    variance = sum((p - ma20) ** 2 for p in closes[-period:]) / period
    std = variance ** 0.5

    upper = ma20 + std_mult * std
    lower = ma20 - std_mult * std
    bandwidth = (upper - lower) / ma20 * 100 if ma20 > 0 else 0
    position = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5

    upper, lower, bandwidth = round(upper, 2), round(lower, 2), round(bandwidth, 1)
    position = round(position, 2)

    if position > 0.9:
        signal = "upper_band"
    elif position < 0.1:
        signal = "lower_band"
    elif position > 0.5:
        signal = "above_middle"
    else:
        signal = "below_middle"

    return {"upper": upper, "middle": round(ma20, 2), "lower": lower,
            "bandwidth": bandwidth, "position": position, "signal": signal}


def calc_volume(volumes: list[float], closes: list[float]) -> dict:
    """Volume analysis: latest vs 5-day MA."""
    if len(volumes) < 6:
        return {"ratio": 1.0, "signal": "insufficient_data"}

    vol_ma5 = sum(volumes[-6:-1]) / 5
    vol_today = volumes[-1]
    ratio = round(vol_today / vol_ma5, 1) if vol_ma5 > 0 else 1.0

    price_chg = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0

    if ratio > 1.5 and price_chg > 0:
        signal = "surge_up"
    elif ratio > 1.5 and price_chg < 0:
        signal = "surge_down"
    elif ratio < 0.5:
        signal = "shrink"
    else:
        signal = "normal"

    return {"ratio": ratio, "signal": signal}


def calc_fibonacci(highs: list[float], lows: list[float], window: int = 60) -> dict[str, float]:
    """Calculate Fibonacci retracement levels from recent swing high/low."""
    if len(highs) < window or len(lows) < window:
        return {}

    recent_high = max(highs[-window:])
    recent_low = min(lows[-window:])
    diff = recent_high - recent_low

    if diff <= 0:
        return {}

    levels = {
        "high": round(recent_high, 2),
        "low": round(recent_low, 2),
        "0.0%": round(recent_high, 2),
        "23.6%": round(recent_high - diff * 0.236, 2),
        "38.2%": round(recent_high - diff * 0.382, 2),
        "50.0%": round(recent_high - diff * 0.5, 2),
        "61.8%": round(recent_high - diff * 0.618, 2),
        "78.6%": round(recent_high - diff * 0.786, 2),
        "100.0%": round(recent_low, 2),
    }
    return levels


def calc_support_resistance(highs: list[float], lows: list[float], closes: list[float]) -> tuple[list[float], list[float]]:
    """Identify swing lows (supports) and swing highs (resistances)."""
    if len(closes) < 10:
        return [], []

    supports, resistances = [], []
    for i in range(5, len(closes) - 5):
        if closes[i] == min(closes[i - 5:i + 6]):
            supports.append(closes[i])
        if closes[i] == max(closes[i - 5:i + 6]):
            resistances.append(closes[i])

    # Deduplicate and sort
    supports = sorted(set(round(s, 2) for s in supports[-3:]))
    resistances = sorted(set(round(r, 2) for r in resistances[-3:]))

    return supports, resistances


# ============================================================================
# Main: Compute all indicators for one symbol
# ============================================================================

def compute_all(symbol: str, kline_data: list[dict]) -> IndicatorResult:
    """Compute all technical indicators from kline data.

    Args:
        symbol: Stock symbol.
        kline_data: List of OHLCV dicts with 'close', 'high', 'low', 'volume' keys.

    Returns:
        IndicatorResult with all computed values.
    """
    result = IndicatorResult(symbol=symbol)

    if not kline_data or len(kline_data) < 20:
        result.date = kline_data[-1]["date"] if kline_data else ""
        return result

    closes = [b["close"] for b in kline_data]
    highs = [b["high"] for b in kline_data]
    lows = [b["low"] for b in kline_data]
    volumes = [b["volume"] for b in kline_data]

    result.date = kline_data[-1]["date"]
    result.close = closes[-1]

    # MA
    ma = calc_ma(closes)
    result.ma5 = ma["ma5"]
    result.ma10 = ma["ma10"]
    result.ma20 = ma["ma20"]
    result.ma60 = ma["ma60"]
    result.ma_alignment = ma["alignment"]

    # RSI
    rsi = calc_rsi(closes)
    result.rsi14 = rsi["value"]
    result.rsi_signal = rsi["signal"]

    # MACD
    macd = calc_macd(closes)
    result.macd_diff = macd["diff"]
    result.macd_dea = macd["dea"]
    result.macd_histogram = macd["histogram"]
    result.macd_signal = macd["signal"]

    # KDJ
    kdj = calc_kdj(highs, lows, closes)
    result.kdj_k = kdj["k"]
    result.kdj_d = kdj["d"]
    result.kdj_j = kdj["j"]
    result.kdj_signal = kdj["signal"]

    # Bollinger
    bb = calc_bollinger(closes)
    result.bb_upper = bb["upper"]
    result.bb_middle = bb["middle"]
    result.bb_lower = bb["lower"]
    result.bb_bandwidth = bb["bandwidth"]
    result.bb_position = bb["position"]
    result.bb_signal = bb["signal"]

    # Volume
    vol = calc_volume(volumes, closes)
    result.vol_ratio = vol["ratio"]
    result.vol_signal = vol["signal"]

    # Fibonacci
    result.fib_levels = calc_fibonacci(highs, lows)

    # Support / Resistance
    result.supports, result.resistances = calc_support_resistance(highs, lows, closes)

    # Resonance: count bullish/neutral/bearish across 6 indicators
    indicators = [
        (ma["alignment"], "bullish" if ma["alignment"] == "bullish" else ("bearish" if ma["alignment"] == "bearish" else "neutral")),
        (macd["signal"], "bullish" if macd["signal"] == "golden_cross" else ("bearish" if macd["signal"] == "death_cross" else "neutral")),
        (kdj["signal"], "bullish" if kdj["signal"] in ("oversold", "bullish") else ("bearish" if kdj["signal"] in ("overbought", "bearish") else "neutral")),
        (rsi["signal"], "bullish" if rsi["signal"] in ("oversold", "bullish") else ("bearish" if rsi["signal"] == "overbought" else "neutral")),
        (vol["signal"], "bullish" if vol["signal"] == "surge_up" else ("bearish" if vol["signal"] == "surge_down" else "neutral")),
        (bb["signal"], "bullish" if bb["signal"] == "lower_band" else ("bearish" if bb["signal"] == "upper_band" else "neutral")),
    ]

    result.bullish_count = sum(1 for _, s in indicators if s == "bullish")
    result.neutral_count = sum(1 for _, s in indicators if s == "neutral")
    result.bearish_count = sum(1 for _, s in indicators if s == "bearish")

    # Overall
    b = result.bullish_count
    if b >= 4:
        result.overall = "bullish"
    elif b >= 3:
        result.overall = "mildly_bullish"
    elif result.bearish_count >= 4:
        result.overall = "bearish"
    elif result.bearish_count >= 3:
        result.overall = "mildly_bearish"
    else:
        result.overall = "neutral"

    # Trend strength
    if result.overall in ("bullish", "mildly_bullish"):
        result.trend_strength = "多头"
    elif result.overall in ("bearish", "mildly_bearish"):
        result.trend_strength = "空头"
    else:
        result.trend_strength = "震荡"

    return result


# ============================================================================
# Batch processing
# ============================================================================

def process_date(date_str: str, data_root: Optional[Path] = None) -> dict[str, Any]:
    """Compute indicators for all symbols with quote data on a given date.

    Args:
        date_str: YYYY-MM-DD date string.
        data_root: Root data directory (defaults to ../data relative to this script).

    Returns:
        {"computed": N, "errors": [...]}
    """
    if data_root is None:
        data_root = Path(__file__).resolve().parent.parent / "data"

    quotes_dir = data_root / date_str / "quotes"
    indicators_dir = data_root / date_str / "indicators"
    indicators_dir.mkdir(parents=True, exist_ok=True)

    if not quotes_dir.exists():
        logger.warning("No quote data for %s", date_str)
        return {"computed": 0, "errors": [f"No quote data for {date_str}"]}

    errors = []
    computed = 0

    for qf in sorted(quotes_dir.glob("*.json")):
        symbol = qf.stem
        try:
            kline = json.loads(qf.read_text(encoding="utf-8"))
            result = compute_all(symbol, kline)

            out_path = indicators_dir / f"{symbol}.json"
            out_path.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            computed += 1
            logger.info("  %s: %s (RSI=%.1f, %s)", symbol, result.overall, result.rsi14, result.trend_strength)
        except Exception as e:
            errors.append(f"{symbol}: {e}")
            logger.warning("  %s: ERROR — %s", symbol, str(e)[:80])

    logger.info("Indicators: %d computed, %d errors", computed, len(errors))
    return {"computed": computed, "errors": errors}


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Compute technical indicators")
    parser.add_argument("--date", type=str, required=True, help="Date YYYY-MM-DD")
    parser.add_argument("--data-root", type=str, default=None, help="Data root directory")
    args = parser.parse_args()

    root = Path(args.data_root) if args.data_root else None
    result = process_date(args.date, root)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        import sys
        sys.exit(1)

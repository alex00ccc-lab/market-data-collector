"""Trading calendar and utility functions for market-data-collector.

Standalone — no dependency on personal_agent project.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Beijing timezone
TZ_BEIJING = timezone(timedelta(hours=8))

# Chinese (A-share) public holidays 2020-2026 — weekday closures only.
# Weekends are already excluded by the weekend check in is_trading_day().
# Sources: official 国务院 holiday announcements. Weekday closures listed;
# Saturday/Sunday within a holiday window are omitted (redundant with weekend check).
CN_HOLIDAYS = {
    # ── 2020 ──
    "2020-01-01",                                      # 元旦
    "2020-01-24", "2020-01-27", "2020-01-28", "2020-01-29", "2020-01-30", "2020-01-31",  # 春节 (extended, COVID)
    "2020-04-06",                                      # 清明
    "2020-05-01", "2020-05-04", "2020-05-05",           # 劳动节
    "2020-06-25", "2020-06-26",                         # 端午
    "2020-10-01", "2020-10-02", "2020-10-05", "2020-10-06", "2020-10-07", "2020-10-08",  # 国庆+中秋
    # ── 2021 ──
    "2021-01-01",                                      # 元旦
    "2021-02-11", "2021-02-12", "2021-02-15", "2021-02-16", "2021-02-17",  # 春节
    "2021-04-05",                                      # 清明
    "2021-05-03", "2021-05-04", "2021-05-05",           # 劳动节
    "2021-06-14",                                      # 端午
    "2021-09-20", "2021-09-21",                         # 中秋
    "2021-10-01", "2021-10-04", "2021-10-05", "2021-10-06", "2021-10-07",  # 国庆
    # ── 2022 ──
    "2022-01-03",                                      # 元旦
    "2022-01-31", "2022-02-01", "2022-02-02", "2022-02-03", "2022-02-04",  # 春节
    "2022-04-04", "2022-04-05",                         # 清明
    "2022-05-02", "2022-05-03", "2022-05-04",           # 劳动节
    "2022-06-03",                                      # 端午
    "2022-09-12",                                      # 中秋
    "2022-10-03", "2022-10-04", "2022-10-05", "2022-10-06", "2022-10-07",  # 国庆
    # ── 2023 ──
    "2023-01-02",                                      # 元旦
    "2023-01-23", "2023-01-24", "2023-01-25", "2023-01-26", "2023-01-27",  # 春节
    "2023-04-05",                                      # 清明
    "2023-05-01", "2023-05-02", "2023-05-03",           # 劳动节
    "2023-06-22", "2023-06-23",                         # 端午
    "2023-09-29", "2023-10-02", "2023-10-03", "2023-10-04", "2023-10-05", "2023-10-06",  # 中秋+国庆
    # ── 2024 ──
    "2024-01-01",                                      # 元旦
    "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",  # 春节
    "2024-04-04", "2024-04-05",                         # 清明
    "2024-05-01", "2024-05-02", "2024-05-03",           # 劳动节
    "2024-06-10",                                      # 端午
    "2024-09-16", "2024-09-17",                         # 中秋
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",  # 国庆
    # ── 2025 ──
    "2025-01-01",                                      # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",  # 春节
    "2025-04-04",                                      # 清明
    "2025-05-01", "2025-05-02", "2025-05-05",           # 劳动节
    "2025-06-02",                                      # 端午
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",  # 中秋+国庆
    # ── 2026 ──
    "2026-01-01", "2026-01-02",                         # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春节
    "2026-04-05", "2026-04-06",                         # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-22", "2026-06-23", "2026-06-24",           # 端午
    "2026-09-25",                                      # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
}

# US market holidays 2026 (NYSE)
US_HOLIDAYS_2026 = {
    "2026-01-01",   # New Year's Day
    "2026-01-19",   # Martin Luther King Jr. Day
    "2026-02-16",   # Presidents' Day
    "2026-04-03",   # Good Friday
    "2026-05-25",   # Memorial Day
    "2026-06-19",   # Juneteenth
    "2026-07-03",   # Independence Day (observed)
    "2026-09-07",   # Labor Day
    "2026-11-26",   # Thanksgiving
    "2026-12-25",   # Christmas
}

HK_HOLIDAYS_2026 = {
    "2026-01-01",   # New Year's Day
    "2026-02-17", "2026-02-18", "2026-02-19",  # Lunar New Year
    "2026-04-03", "2026-04-04", "2026-04-06",  # Ching Ming + Easter
    "2026-05-01",   # Labour Day
    "2026-06-22",   # Dragon Boat Festival
    "2026-10-01",   # National Day
    "2026-10-21",   # Chung Yeung Festival
    "2026-12-25",   # Christmas
}

# Japan (TSE) market holidays 2026
JP_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02",   # New Year's
    "2026-01-12",                  # Coming of Age Day
    "2026-02-11",                  # National Foundation Day
    "2026-02-23",                  # Emperor's Birthday
    "2026-03-20",                  # Vernal Equinox
    "2026-04-29",                  # Showa Day
    "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",  # Golden Week
    "2026-07-20",                  # Marine Day
    "2026-08-11",                  # Mountain Day
    "2026-09-21",                  # Respect for the Aged Day
    "2026-09-23",                  # Autumnal Equinox
    "2026-10-12",                  # Sports Day
    "2026-11-03",                  # Culture Day
    "2026-11-23",                  # Labor Thanksgiving
    "2026-12-31",                  # New Year's Eve (half day)
}

# European exchanges (Stockholm, Frankfurt, etc.) — 2026 holidays.
# Kept minimal: yfinance gracefully returns the last trading day's close on
# exchange holidays, so the weekend check is the only hard requirement.
EU_HOLIDAYS_2026 = set()


class TradingCalendar:
    """Check if a given date is a trading day for a specific market."""

    def __init__(self):
        self._cn_holidays = CN_HOLIDAYS
        self._us_holidays = US_HOLIDAYS_2026
        self._hk_holidays = HK_HOLIDAYS_2026
        self._jp_holidays = JP_HOLIDAYS_2026
        self._eu_holidays = EU_HOLIDAYS_2026

    def is_trading_day(self, market: str, d: Optional[date] = None) -> bool:
        """Check if `d` is a trading day for the market."""
        if d is None:
            d = datetime.now(TZ_BEIJING).date()

        # Weekend check
        if market in ("A", "HK", "JP"):
            if d.weekday() >= 5:  # Sat/Sun
                return False
            if market == "A":
                holidays = self._cn_holidays
            elif market == "HK":
                holidays = self._hk_holidays
            else:
                holidays = self._jp_holidays
        elif market in ("US", "EU"):
            if d.weekday() >= 5:
                return False
            holidays = self._us_holidays if market == "US" else self._eu_holidays
        else:
            return True

        return d.isoformat() not in holidays

    def should_fetch(self, market: str, d: Optional[date] = None) -> bool:
        """Check if we should attempt to fetch data.

        Returns True if it's a trading day (or Saturday for US Friday close)
        AND the market close has already happened (time-of-day check).

        Previously this was date-only, which caused Monday 08:05 BJT US fetches
        to run before the Monday US session had even opened (BJT Monday evening).
        Now wired to in_fetch_window() for time-of-day awareness.
        """
        if d is None:
            d = datetime.now(TZ_BEIJING).date()

        # On a trading day: check time-of-day
        if self.is_trading_day(market, d):
            now = datetime.now(TZ_BEIJING)
            in_window, label = self.in_fetch_window(market, now)
            if label == "pre_close":
                # Market hasn't closed yet today.
                # For US: allow if yesterday was a trading day
                # (fetching the previous session's close, e.g. Tue 08:05
                # fetching Mon's close which happened ~Tue 05:00 BJT)
                if market in ("US", "EU"):
                    yesterday = d - timedelta(days=1)
                    if self.is_trading_day(market, yesterday):
                        return True
                return False
            return True  # post_close or off_hours

        # On Saturday Beijing time: US Friday data might be available
        if market == "US" and d.weekday() == 5:  # Saturday
            # Check if Friday was a trading day
            friday = d - timedelta(days=1)
            return self.is_trading_day(market, friday)

        return False

    def last_trading_day(self, market: str, before: Optional[date] = None) -> date:
        """Get the most recent trading day on or before `before`."""
        if before is None:
            before = datetime.now(TZ_BEIJING).date()
        d = before
        for _ in range(10):  # Safety limit
            if self.is_trading_day(market, d):
                return d
            d -= timedelta(days=1)
        return before

    def in_fetch_window(self, market: str, now: Optional[datetime] = None) -> tuple[bool, str]:
        """Check if current time is in the optimal fetch window.

        Returns:
            (in_window, label) — label: 'pre_close', 'post_close', 'off_hours'
        """
        if now is None:
            now = datetime.now(TZ_BEIJING)

        close_hour = {"A": 15, "HK": 16, "US": 5, "JP": 14, "EU": 5}.get(market, 15)  # JP: 15:00 JST = 14:00 BJT; EU closes overnight (~00:30 BJT) → treated like US
        current_hour = now.hour + now.minute / 60.0

        if current_hour < close_hour:
            return False, "pre_close"
        elif current_hour < close_hour + 3:  # Within 3h after close
            return True, "post_close"
        else:
            return False, "off_hours"


# ============================================================================
# Utility: Retry decorator
# ============================================================================

def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Decorator: retry a function on failure with exponential backoff."""
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    result = func(*args, **kwargs)
                    if result is not None:
                        return result
                    logger.debug("%s attempt %d: returned None", func.__name__, attempt)
                except Exception as e:
                    last_error = e
                    logger.debug("%s attempt %d: %s", func.__name__, attempt, str(e)[:60])
                if attempt < max_attempts:
                    time.sleep(current_delay)
                    current_delay *= backoff
            logger.warning("%s: all %d attempts failed", func.__name__, max_attempts)
            return None
        return wrapper
    return decorator


# ============================================================================
# Utility: Rate limiter
# ============================================================================

class RateLimiter:
    """Simple rate limiter for API calls."""

    def __init__(self, min_interval: float = 0.5):
        self._min_interval = min_interval
        self._last_call: float = 0.0

    def wait(self):
        """Wait if needed to respect min_interval."""
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()


# ============================================================================
# Utility: Safe JSON save
# ============================================================================

def save_json(data: Any, path: str) -> None:
    """Save data as JSON, creating directories as needed."""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        __import__("json").dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

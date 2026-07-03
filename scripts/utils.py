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

# Chinese public holidays 2026 (approximate — update yearly)
# Format: "YYYY-MM-DD"
CN_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02",           # 元旦
    "2026-01-28", "2026-01-29", "2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02", "2026-02-03",  # 春节
    "2026-04-05", "2026-04-06",           # 清明节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-22", "2026-06-23", "2026-06-24",  # 端午节
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆+中秋
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


class TradingCalendar:
    """Check if a given date is a trading day for a specific market."""

    def __init__(self):
        self._cn_holidays = CN_HOLIDAYS_2026
        self._us_holidays = US_HOLIDAYS_2026
        self._hk_holidays = HK_HOLIDAYS_2026
        self._jp_holidays = JP_HOLIDAYS_2026

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
        elif market == "US":
            if d.weekday() >= 5:
                return False
            holidays = self._us_holidays
        else:
            return True

        return d.isoformat() not in holidays

    def should_fetch(self, market: str, d: Optional[date] = None) -> bool:
        """Check if we should attempt to fetch data.

        Returns True if it's a trading day OR if data might still be available
        (e.g., Friday US data available on Saturday morning Beijing time).
        """
        if d is None:
            d = datetime.now(TZ_BEIJING).date()

        # On a trading day: always fetch
        if self.is_trading_day(market, d):
            return True

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

        close_hour = {"A": 15, "HK": 16, "US": 5, "JP": 14}.get(market, 15)  # JP: 15:00 JST = 14:00 BJT
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

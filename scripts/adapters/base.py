"""Base adapter interface and shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdapterResult:
    """Standard result from any data adapter fetch."""

    symbol: str
    source: str = ""
    success: bool = False
    kline: list[dict] = field(default_factory=list)
    realtime: Optional[dict] = None
    fundamentals: Optional[dict] = None
    error: str = ""
    bars: int = 0


class BaseAdapter(ABC):
    """Abstract base for all data source adapters.

    Each adapter wraps one data provider (yfinance, stooq, etc.) and exposes
    a uniform interface for fetching OHLCV data, real-time quotes, and
    fundamental snapshots.

    Subclasses must implement:
      - name       (property)
      - fetch_kline(symbol, market, days) -> Optional[list[dict]]
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logs and health reports (e.g. 'yfinance')."""
        ...

    @abstractmethod
    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        """Fetch daily OHLCV bars.

        Args:
            symbol: Ticker (e.g. 'TSLA', '300476.SZ', '6981.T').
            market: 'US' | 'A' | 'HK' | 'JP'.
            days: Approximate number of trading days to fetch.

        Returns:
            List of dicts with keys: date, open, high, low, close, volume, source.
            None if the adapter cannot service this symbol/market.
        """
        ...

    def fetch_realtime(self, symbol: str, market: str) -> Optional[dict]:
        """Fetch a real-time / latest quote snapshot.

        Returns dict with: symbol, price, change_pct, volume, source, timestamp.
        Default implementation returns None (not all adapters support this).
        """
        return None

    def fetch_fundamentals(self, symbol: str, market: str) -> Optional[dict]:
        """Fetch fundamental data (PE, PB, ROE, market cap, etc.).

        Default implementation returns None.
        """
        return None

    def supports_market(self, market: str) -> bool:
        """Override to declare which markets this adapter handles.

        Default: all markets.
        """
        return True

    def health_check(self) -> bool:
        """Quick connectivity test. Default: True (assume OK until proven otherwise)."""
        return True

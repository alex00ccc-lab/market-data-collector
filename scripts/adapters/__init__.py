"""Multi-source data adapters for market-data-collector.

Each adapter wraps one data source (yfinance, stooq, efinance, alpha_vantage)
with a common interface so the SourceManager can switch between them automatically.
"""

from .base import BaseAdapter, AdapterResult
from .yfinance_adapter import YFinanceAdapter
from .stooq_adapter import StooqAdapter
from .efinance_adapter import EFinanceAdapter

__all__ = [
    "BaseAdapter",
    "AdapterResult",
    "YFinanceAdapter",
    "StooqAdapter",
    "EFinanceAdapter",
]

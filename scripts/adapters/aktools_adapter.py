"""AkTools adapter — free A-share data via Sina/Tencent sources.

Alternative to efinance when East Money API is geo-blocked.
Uses ``akshare`` library which reads from Sina Finance and Tencent Finance —
typically accessible from outside China.

Install: pip install akshare
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, LOG_TRUNCATE_LENGTH

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 0.5


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


class AkToolsAdapter(BaseAdapter):
    """Fetches A-share daily K-line via akshare (Sina/Tencent source).

    No API key required.  Uses Sina Finance and Tencent Finance as upstream —
    these are typically accessible from outside mainland China, making this
    a viable fallback when efinance (East Money) is geo-blocked.
    """

    @property
    def name(self) -> str:
        return "aktools"

    def supports_market(self, market: str) -> bool:
        return market == "A"

    def _import_ak(self):
        """Lazy-import akshare (heavy dependency)."""
        try:
            import akshare as ak  # noqa: F401
            return ak
        except ImportError:
            logger.warning("aktools: akshare not installed — pip install akshare")
            return None

    def _clean_symbol(self, symbol: str) -> str:
        """Normalize A-share symbol: strip .SH/.SZ suffix for akshare."""
        return symbol.upper().replace(".SH", "").replace(".SZ", "")

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        ak = self._import_ak()
        if ak is None:
            return None

        clean = self._clean_symbol(symbol)
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        _rate_limit()
        try:
            df = ak.stock_zh_a_hist(
                symbol=clean,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
        except Exception as e:
            logger.debug("aktools(%s): request error — %s", symbol, str(e)[:LOG_TRUNCATE_LENGTH])
            return None

        if df is None or df.empty:
            logger.debug("aktools(%s): empty response", symbol)
            return None

        bars = []
        for _, row in df.iterrows():
            # Filter NaN close values (non-trading days, data gaps)
            close_val = row.get("收盘")
            if close_val is None:
                continue
            try:
                close_f = float(close_val)
            except (ValueError, TypeError):
                continue
            if close_f <= 0:
                continue

            bars.append({
                "date": str(row.get("日期", ""))[:10],
                "open": float(row.get("开盘", 0) or 0),
                "high": float(row.get("最高", 0) or 0),
                "low": float(row.get("最低", 0) or 0),
                "close": close_f,
                "volume": float(row.get("成交量", 0) or 0),
                "source": "aktools",
            })

        if bars:
            logger.info("aktools(%s) OK — %d bars", symbol, len(bars))
        return bars if bars else None

    def health_check(self) -> bool:
        """Quick connectivity test using a well-known A-share ticker."""
        ak = self._import_ak()
        if ak is None:
            return False
        try:
            df = ak.stock_zh_a_hist(
                symbol="000001",
                period="daily",
                start_date=(datetime.now() - timedelta(days=5)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",
            )
            return df is not None and not df.empty
        except Exception:
            return False

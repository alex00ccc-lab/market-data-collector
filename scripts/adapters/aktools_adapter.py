"""AkTools adapter — free A-share / HK stock data via akshare.

Alternative to efinance when the East Money API is unreachable or rate-limited.
``akshare`` aggregates several upstream providers (East Money, Sina, Tencent);
this adapter uses its A-share and HK daily-K interfaces, which are typically
accessible from mainland China.

Install: pip install akshare
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from typing import Optional

from .base import BaseAdapter
from utils.constants import TZ_BEIJING, LOG_TRUNCATE_LENGTH
from utils.exceptions import AdapterNotAvailableError

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 0.5

# Minimum akshare version (as a comparable tuple — fixes M16: string
# comparison mis-orders versions like "1.9.0" vs "1.14.0").
_MIN_VER = (1, 14, 0)


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


def _version_tuple(version) -> tuple:
    """Parse a dotted version string into an int tuple for correct comparison."""
    try:
        return tuple(int(x) for x in str(version).split("."))
    except (ValueError, TypeError):
        return (0,)


class AkToolsAdapter(BaseAdapter):
    """Fetches daily K-line for A-shares and HK stocks via akshare.

    No API key required.  A-share path uses ``stock_zh_a_hist``; HK path uses
    ``stock_hk_hist``.  Serves as the local fallback when efinance (East Money)
    fails, so HK fetch keeps working from a mainland-China host.
    """

    @property
    def name(self) -> str:
        return "aktools"

    def supports_market(self, market: str) -> bool:
        return market in ("A", "HK")

    def _import_ak(self):
        """Lazy-import akshare (heavy dependency) with version check.

        Raises AdapterNotAvailableError when akshare is missing or too old, so
        SourceManager can record "unavailable" (依赖缺失) — distinct from a
        runtime fetch failure (抓取失败) that would warrant a retry.
        """
        try:
            import akshare as ak
        except ImportError as e:
            raise AdapterNotAvailableError(
                "akshare not installed — pip install akshare>=1.14.0"
            ) from e
        if hasattr(ak, "__version__") and _version_tuple(ak.__version__) < _MIN_VER:
            raise AdapterNotAvailableError(
                f"akshare version {ak.__version__} too old "
                f"(min {'.'.join(map(str, _MIN_VER))}) — pip install -U akshare"
            )
        return ak

    def _clean_symbol(self, symbol: str) -> str:
        """Normalize A-share symbol: strip .SH/.SZ suffix for akshare."""
        return symbol.upper().replace(".SH", "").replace(".SZ", "")

    def _clean_hk_symbol(self, symbol: str) -> str:
        """Normalize HK symbol: strip .HK suffix and zero-pad to 5 digits.

        akshare ``stock_hk_hist`` expects a 5-digit code with leading zero,
        e.g. ``9992.HK`` → ``09992``.
        """
        code = symbol.upper().replace(".HK", "").strip()
        return code.zfill(5)

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        ak = self._import_ak()
        if ak is None:
            return None

        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        _rate_limit()
        try:
            if market == "HK":
                code = self._clean_hk_symbol(symbol)
                df = ak.stock_hk_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
            else:
                clean = self._clean_symbol(symbol)
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

        # Both stock_zh_a_hist and stock_hk_hist return Chinese columns:
        # 日期/开盘/收盘/最高/最低/成交量/成交额 — shared mapping below.
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
        try:
            ak = self._import_ak()
        except AdapterNotAvailableError:
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

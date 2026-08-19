"""mootdx adapter — A-share daily K-line via the 通达信 (TDX) TCP protocol.

Independent transport from Tencent (HTTP) and East Money (HTTP) — a raw TCP
connection to TDX quote servers, so it is not subject to the same residential-IP
HTTP blocking that hits efinance.  Serves as the A-share fallback behind Tencent.

NOTE (2026-08-19): the free TDX server pool has largely gone stale — only a
minority of bundled servers still accept TCP, and of those, most return empty
handshakes.  This adapter is wired in as best-effort: it degrades to None (→
SourceManager "failed") when no server responds, and will resume contributing
automatically once the server pool recovers.  Not used for HK (TDX HK history
K-line is immature).

Install: pip install mootdx  (pulls tdxpy, pandas)
"""

from __future__ import annotations

import logging
import time as _time
from typing import Optional

from .base import BaseAdapter
from utils.constants import LOG_TRUNCATE_LENGTH
from utils.exceptions import AdapterNotAvailableError

logger = logging.getLogger(__name__)

_last_call = 0.0
_MIN_INTERVAL = 0.5

# Cached TDX client (created once — TCP connect is the expensive part).  A
# ``None`` result is cached too, so a dead pool only incurs one connect attempt.
_UNSET = object()
_client = _UNSET


def _rate_limit():
    global _last_call
    elapsed = _time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        _time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = _time.time()


class MootdxAdapter(BaseAdapter):
    """Fetches daily K-line for A-shares via the 通达信 TCP protocol."""

    @property
    def name(self) -> str:
        return "mootdx"

    def supports_market(self, market: str) -> bool:
        return market == "A"

    def _import_mootdx(self):
        """Lazy-import mootdx (heavy TCP dependency).

        Raises AdapterNotAvailableError when missing, so SourceManager records
        "unavailable" (依赖缺失) — distinct from a runtime fetch failure.
        """
        try:
            import mootdx  # noqa: F401
            from mootdx.quotes import Quotes
            return Quotes
        except ImportError as e:
            raise AdapterNotAvailableError(
                "mootdx not installed — pip install mootdx"
            ) from e

    def _clean_symbol(self, symbol: str) -> str:
        """Strip .SH/.SZ suffix → bare 6-digit code (e.g. 513010.SH → 513010)."""
        return symbol.upper().replace(".SH", "").replace(".SZ", "").strip()

    def _get_client(self):
        global _client
        if _client is not _UNSET:
            return _client
        try:
            Quotes = self._import_mootdx()
            # bestip=False uses the default server; a short timeout bounds the
            # connect cost when the pool is stale.  Never pass bestip=True here —
            # it requires a pre-populated ``~/.mootdx/config.json`` (BESTIP.HQ)
            # and crashes otherwise.
            _client = Quotes.factory(market="std", bestip=False, timeout=5)
        except Exception as e:
            logger.debug("mootdx: client init failed — %s", str(e)[:LOG_TRUNCATE_LENGTH])
            _client = None
        return _client

    def fetch_kline(self, symbol: str, market: str, days: int = 120) -> Optional[list[dict]]:
        self._import_mootdx()  # raise AdapterNotAvailableError if missing
        client = self._get_client()
        if client is None:
            return None

        code = self._clean_symbol(symbol)
        _rate_limit()
        try:
            df = client.bars(symbol=code, frequency=9, offset=min(days, 800))
        except Exception as e:
            logger.debug("mootdx(%s): request error — %s", symbol, str(e)[:LOG_TRUNCATE_LENGTH])
            return None

        if df is None or df.empty:
            logger.debug("mootdx(%s): empty response", symbol)
            return None

        # TDX daily-bar DataFrame: index = datetime, columns open/close/high/low/vol(+volume).
        bars = []
        for idx, row in df.iterrows():
            close_val = row.get("close")
            if close_val is None:
                continue
            try:
                close_f = float(close_val)
            except (ValueError, TypeError):
                continue
            if close_f <= 0:
                continue

            date_str = str(row.get("datetime", "") or "")[:10] or str(idx)[:10]
            bars.append({
                "date": date_str,
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": close_f,
                "volume": float(row.get("volume", row.get("vol", 0)) or 0),
                "source": "mootdx",
            })

        if bars:
            logger.info("mootdx(%s) OK — %d bars", symbol, len(bars))
        return bars if bars else None

    def health_check(self) -> bool:
        """Quick connectivity test using a well-known A-share ticker."""
        try:
            self._import_mootdx()
            client = self._get_client()
            if client is None:
                return False
            df = client.bars(symbol="000001", frequency=9, offset=2)
            return df is not None and not df.empty
        except Exception:
            return False

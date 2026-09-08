"""One-off test: can stooq or Alpha Vantage serve same-day JP (TSE) daily OHLCV?

Run:  python market_data/scripts/test_jp_sources.py

Context (2026-09-08): 6981.T 报「all sources failed (tried: yfinance, yahoo_chart)」
——JP 有效源链只剩 Yahoo 同 IP 池两个源，周期封禁时双双失败。本脚本对两个
从未做过结论性 JP 实测的免费源做一次性实测：

  - stooq（2026-08-06 因 Cloudflare 禁用）：6981.jp 的 CSV 端点是否仍被拦？
  - Alpha Vantage（配置假设 US-only）：TIME_SERIES_DAILY 能否返回 6981 近期日足？

判据：返回非陈旧（近 ~6 自然日）日足 = WORKS，可作 JP fallback；否则维持接受缺口。
不写任何适配器代码、不改任何配置。只读 alpha_vantage_api_key（不打印值）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# 允许 `from key_loader import get_key`（本脚本与 key_loader 同目录）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20
TODAY = date.today()


def _http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def _recent(datestr: str, days: int = 6) -> bool:
    """近 ~6 自然日（≈5 交易日）内 = 非陈旧。"""
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d").date()
        return (TODAY - d).days <= days
    except Exception:  # noqa: BLE001
        return False


def test_stooq(symbol: str = "6981.jp") -> str:
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    status, body = _http_get(url)
    if status != 200:
        return f"BLOCKED/ERROR (HTTP {status})"
    head = body[:400].strip()
    lowered = body.lower()
    if "Date" in head and ("Open" in head):
        lines = [l for l in body.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return "NO_DATA (header only)"
        latest = lines[-1].split(",")[0]
        return f"WORKS (latest={latest})" if _recent(latest) else f"STALE (latest={latest})"
    if "cloudflare" in lowered or "challenge" in lowered or "<html" in lowered:
        return "BLOCKED (Cloudflare/HTML challenge)"
    return f"ERROR (unexpected body: {head[:80]!r})"


def test_alpha_vantage() -> str:
    try:
        from key_loader import get_key
    except Exception:  # noqa: BLE001
        import yaml

        def get_key(name: str, default: str = "") -> str:
            p = Path(__file__).resolve().parent.parent / "config" / "keys.yaml"
            if not p.exists():
                return default
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            val = (data or {}).get(name, default)
            return val if isinstance(val, str) and val.strip() else default

    key = get_key("alpha_vantage_api_key", "")
    if not key:
        return "NO_KEY (alpha_vantage_api_key not configured)"

    for sym in ("6981.T", "TSE:6981", "6981.TOK"):
        url = ("https://www.alphavantage.co/query"
               f"?function=TIME_SERIES_DAILY&symbol={urllib.parse.quote(sym)}"
               f"&outputsize=compact&apikey={key}")
        status, body = _http_get(url)
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return f"ERROR (non-JSON, HTTP {status})"
        if "Time Series (Daily)" in data:
            dates = sorted(data["Time Series (Daily)"].keys(), reverse=True)
            latest = dates[0] if dates else "?"
            tag = "WORKS" if _recent(latest) else "STALE"
            return f"{tag} sym={sym} (latest={latest})"
        if "Information" in data:
            return f"RATE_LIMITED ({data['Information']})"
        if "Note" in data:
            # 免费档「请扩散」说明——若伴随 Time Series 已在上面返回；这里无数据则如实标注。
            return f"NO_DATA (note: {data['Note'][:120]})"
        # "Error Message" (invalid symbol) → 试下一个符号格式
        continue
    return "NO_DATA (all symbol formats failed)"


def main() -> None:
    print(f"=== JP 免费源实测 (today={TODAY}) ===")
    print(f"stooq           : {test_stooq()}")
    print(f"alpha_vantage   : {test_alpha_vantage()}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""options_wall — 期权墙指标（max pain / call wall / put wall / ATM IV / IV-HV）。

数据源：marketdata.app 免费档（``GET /v1/options/chain/{SYM}/``，列过滤后 1 credit/链，
含 strike/openInterest/side/expiration/dte/iv）。现价与 HV20（已实现波动率）从已有的
``data/{date}/quotes/{SYM}.json``（~120d OHLCV）本地算，不重复抓 quote。

输出：``data/{date}/options/{SYM}.json``（镜像 fundamentals 的 per-symbol JSON 模式）。

单源（marketdata.app 是唯一免费期权链源），无 fallback 链——单标的失败即跳过，不阻断整体
（lenient 语义，对齐 fetch-weekly 的「单标的失败不阻塞」原则）。

Usage:
  python scripts/options_wall.py                     # 全部 US 标的（holdings + watchlist）
  python scripts/options_wall.py --symbols MRVL,BE   # 指定标的（测试用）
  python scripts/options_wall.py --date 2026-09-03   # 指定数据日期（默认 BJT 今天）
  python scripts/options_wall.py --dry-run           # 只打印不写盘
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("options_wall")

_TZ_BJ = timezone(timedelta(hours=8))
_ROOT = Path(__file__).resolve().parent.parent  # market_data/
_DATA_DIR = _ROOT / "data"
_CONFIG_DIR = _ROOT / "config"

_CHAIN_COLUMNS = "strike,openInterest,side,expiration,dte,iv"
_CHAIN_URL = "https://api.marketdata.app/v1/options/chain/{symbol}/"
_REQUEST_TIMEOUT = 30
_HV_WINDOW = 20        # realized vol 回看交易日数
_MIN_IV = 0.01         # IV < 1% 视为无效（深虚值 placeholder 0.0001）


def _get_token() -> str:
    from key_loader import get_key
    return get_key("marketdata_app_token", "")


def _load_us_symbols() -> list[dict]:
    """读 holdings.json + watchlist.json 的 US 标的（去重保序）。"""
    seen, out = set(), []
    for name in ("holdings.json", "watchlist.json"):
        p = _CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 读取失败: %s", name, e)
            continue
        for s in cfg.get("symbols", []):
            if s.get("market") != "US":
                continue
            sym = s.get("symbol", "").upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(s)
    return out


def _latest_quote_file(symbol: str, date: str) -> Optional[Path]:
    """找最近的 quotes/{SYM}.json（从 date 往前回退最多 10 天，适配周末/节假日）。"""
    try:
        base = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None
    for i in range(11):
        p = _DATA_DIR / (base - timedelta(days=i)).isoformat() / "quotes" / f"{symbol}.json"
        if p.exists():
            return p
    return None


def _load_closes(symbol: str, date: str) -> tuple[Optional[float], list[float]]:
    """读 quotes JSON → (现价 last_close, closes 序列)。找不到返回 (None, [])。"""
    p = _latest_quote_file(symbol, date)
    if p is None:
        return None, []
    try:
        bars = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("%s quotes 读取失败: %s", symbol, e)
        return None, []
    closes = [
        b["close"] for b in bars
        if isinstance(b, dict) and isinstance(b.get("close"), (int, float)) and b["close"] > 0
    ]
    if not closes:
        return None, []
    return closes[-1], closes


def _realized_vol(closes: list[float], window: int = _HV_WINDOW) -> Optional[float]:
    """年化 realized vol（默认 20 交易日）：std(log returns) × √252。"""
    if len(closes) < 3:
        return None
    xs = closes[-(window + 1):]
    rets = [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs)) if xs[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _fetch_chain(symbol: str, token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        r = requests.get(
            _CHAIN_URL.format(symbol=symbol),
            params={"columns": _CHAIN_COLUMNS},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("%s chain 请求异常: %s", symbol, e)
        return None
    if r.status_code not in (200, 203):
        logger.warning("%s chain HTTP %s: %s", symbol, r.status_code, r.text[:120])
        return None
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        logger.warning("%s chain JSON 解析失败", symbol)
        return None

    keys = _CHAIN_COLUMNS.split(",")
    if not all(isinstance(j.get(k), list) for k in keys):
        logger.warning("%s chain 缺列或非列表: %s", symbol, list(j.keys()))
        return None
    n = len(j["strike"])
    if n == 0 or any(len(j[k]) != n for k in keys):
        logger.warning("%s chain 列长不一致", symbol)
        return None
    return j


def _pick_expiration(j: dict) -> Optional[int]:
    """选总 OI 最高的到期日（最流动，墙信号最有意义）。"""
    groups: dict[int, list[int]] = {}
    for i, exp in enumerate(j["expiration"]):
        groups.setdefault(exp, []).append(i)
    best_exp, best_oi = None, -1
    for exp, idxs in groups.items():
        total = sum(j["openInterest"][i] for i in idxs)
        if total > best_oi:
            best_oi, best_exp = total, exp
    return best_exp


def _compute_metrics(j: dict, exp: int, underlying: Optional[float]) -> dict:
    """在单个到期日 exp 上算 max pain / call wall / put wall / ATM IV。"""
    idxs = [i for i, e in enumerate(j["expiration"]) if e == exp]
    call_oi: dict = {}
    put_oi: dict = {}
    strikes: set = set()
    for i in idxs:
        k = j["strike"][i]
        oi = j["openInterest"][i]
        strikes.add(k)
        if j["side"][i] == "call":
            call_oi[k] = call_oi.get(k, 0) + oi
        else:
            put_oi[k] = put_oi.get(k, 0) + oi

    # max pain：argmin over strikes of Σ max(0,p-k)·callOI + Σ max(0,k-p)·putOI
    all_strikes = sorted(strikes)
    best_p, best_pain = None, None
    for p in all_strikes:
        pain = 0.0
        for k, oi in call_oi.items():
            if p > k:
                pain += (p - k) * oi
        for k, oi in put_oi.items():
            if k > p:
                pain += (k - p) * oi
        if best_pain is None or pain < best_pain:
            best_pain, best_p = pain, p

    call_wall = max(call_oi, key=call_oi.get) if call_oi else None
    put_wall = max(put_oi, key=put_oi.get) if put_oi else None

    # ATM IV：离现价最近的 strike 的 call/put IV 平均（过滤无效 IV）
    atm_iv = None
    if underlying is not None and strikes:
        atm_k = min(all_strikes, key=lambda k: abs(k - underlying))
        ivs = [
            j["iv"][i] for i in idxs
            if j["strike"][i] == atm_k
            and isinstance(j["iv"][i], (int, float)) and j["iv"][i] >= _MIN_IV
        ]
        if ivs:
            atm_iv = sum(ivs) / len(ivs)

    return {
        "max_pain": best_p,
        "call_wall": call_wall,
        "call_wall_oi": call_oi.get(call_wall) if call_wall is not None else None,
        "put_wall": put_wall,
        "put_wall_oi": put_oi.get(put_wall) if put_wall is not None else None,
        "atm_iv": atm_iv,
    }


def _process_symbol(symbol: str, token: str, date: str) -> Optional[dict]:
    close, closes = _load_closes(symbol, date)
    chain = _fetch_chain(symbol, token)
    if chain is None:
        return None
    exp = _pick_expiration(chain)
    if exp is None:
        return None

    m = _compute_metrics(chain, exp, close)
    hv = _realized_vol(closes)
    iv_hv = (m["atm_iv"] - hv) if (m["atm_iv"] is not None and hv is not None) else None

    dte = next((chain["dte"][i] for i, e in enumerate(chain["expiration"]) if e == exp), None)
    exp_date = datetime.fromtimestamp(exp, tz=_TZ_BJ).strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "date": date,
        "underlying_price": round(close, 2) if close is not None else None,
        "max_pain": m["max_pain"],
        "call_wall": m["call_wall"],
        "call_wall_oi": m["call_wall_oi"],
        "put_wall": m["put_wall"],
        "put_wall_oi": m["put_wall_oi"],
        "atm_iv": round(m["atm_iv"], 4) if m["atm_iv"] is not None else None,
        "hv_20d": round(hv, 4) if hv is not None else None,
        "iv_hv_gap": round(iv_hv, 4) if iv_hv is not None else None,
        "expiration": exp_date,
        "dte": dte,
        "source": "marketdata.app",
        "timestamp": datetime.now(_TZ_BJ).isoformat(),
    }


def _pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "—"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="期权墙指标抓取（marketdata.app 免费档）")
    parser.add_argument("--symbols", default=None, help="逗号分隔，覆盖默认的 holdings+watchlist US 标的")
    parser.add_argument("--date", default=None, help="数据日期 YYYY-MM-DD，默认今天(BJT)")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    date = args.date or datetime.now(_TZ_BJ).strftime("%Y-%m-%d")
    token = _get_token()
    if not token:
        logger.error("marketdata_app_token 未配置，退出")
        return 1

    if args.symbols:
        symbols = [{"symbol": s.strip().upper()} for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _load_us_symbols()
    logger.info("期权墙目标 %d 只: %s", len(symbols), [s["symbol"] for s in symbols])

    out_dir = _DATA_DIR / date / "options"
    ok = 0
    for item in symbols:
        sym = item["symbol"]
        data = _process_symbol(sym, token, date)
        if data is None:
            logger.warning("%s: 期权墙数据缺失（无链/无 quotes），跳过", sym)
            continue
        ok += 1
        if not args.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{sym}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        logger.info(
            "  %s: 现价 %s · maxPain %s · callWall %s(OI %s) · putWall %s(OI %s) · "
            "ATM IV %s · HV20 %s · IV-HV %s",
            sym, data["underlying_price"], data["max_pain"], data["call_wall"], data["call_wall_oi"],
            data["put_wall"], data["put_wall_oi"], _pct(data["atm_iv"]), _pct(data["hv_20d"]),
            _pct(data["iv_hv_gap"]),
        )
    logger.info("期权墙完成 %d/%d 只", ok, len(symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

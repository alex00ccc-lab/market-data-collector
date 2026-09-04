# -*- coding: utf-8 -*-
"""macro_regime — 宏观 regime（杠杆 / 流动性 / 预期三力）→ 期权下单门的数据层。

第一性原理：市场不是预测价格，而是资本在「杠杆约束 + 流动性约束 + 预期变化」下不断重定价。
对期权**卖方**，赚的是「约束稳定、预期不变」的钱，亏的是「约束突变、流动性收缩、预期跳变」的钱。
所以 regime 只回答一件事：

    「当前宏观环境，值不值得承担卖方期权的尾部风险？」

——它**不做** GO/AVOID 执行决策（那是 holdings-briefing options_income.py 的活）。
本脚本是纯数据/状态层：JSON 不出现 go/avoid/sell/skip 等执行字段。

数据源：FRED 免费 API（5 序列）+ config/event_calendar.json（FOMC/CPI/PPI 静态）+ 可选
data/{date}/macro.json（VIX/10Y/油价 上下文）。

输出：data/{date}/macro_regime.json —— regime + 每指标 {value, trend(枚举), light, watch}。

Usage:
  python scripts/macro_regime.py                 # 真实 FRED 抓取（需 FRED_API_KEY）
  python scripts/macro_regime.py --mock          # mock FRED 值跑通框架（无 key 测试）
  python scripts/macro_regime.py --date 2026-09-04
  python scripts/macro_regime.py --dry-run       # 只打印不写盘
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("macro_regime")

_TZ_BJ = timezone(timedelta(hours=8))
_ROOT = Path(__file__).resolve().parent.parent  # market_data/
_DATA_DIR = _ROOT / "data"
_CONFIG_DIR = _ROOT / "config"

_FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
_REQUEST_TIMEOUT = 30

# ── 灯严重度（用于「worst」聚合）──────────────────────────────────────────
_SEV = {"🟢": 0, "🟡": 1, "🟠": 2, "🔴": 3}

# ── 阈值（metric_defs，集中定义便于调参）────────────────────────────────────
_T10Y2Y_INVERT = -0.20   # 倒挂阈值（< -0.2 = 衰退慢信号 🔴）
_T10Y2Y_STEEP = 0.20     # 陡峭阈值（> +0.2 = 健康 🟢）
_HYOAS_TIGHT = 4.0       # 信用利差宽松上限（< 4.0 🟢）
_HYOAS_STRESS = 5.5      # 信用承压阈值（> 5.5 🔴）
_WALCL_CONSEC = 8        # 连续缩表周数 ≥ 8 → 🔴
_SOFR_SPIKE_BP = 0.20    # SOFR 跳升 > 近期中位数 +20bp → 🔴
_T10YIE_LOW = 1.8        # 通缩恐慌
_T10YIE_HIGH = 2.8       # 通胀恐慌
_EVENT_WARN_DAYS = 14    # 事件 7–14 天 🟡
_EVENT_NEAR_DAYS = 7     # 事件 < 7 天 🟠

_REGIME_LABEL = {"STABLE": "稳", "NEUTRAL": "中性", "RISK_OFF": "险", "EVENT_NEAR": "事件临近"}

# 序列定义：series_id → (频率, 分类器, 所属力)。WALCL 周频，其余日频。
_SERIES_SPECS = [
    ("T10Y2Y", "daily", "leverage"),
    ("BAMLH0A0HYM2", "daily", "leverage"),
    ("WALCL", "weekly", "liquidity"),
    ("SOFR", "daily", "liquidity"),
    ("T10YIE", "daily", "expectations"),
]


def _get_fred_key() -> str:
    from key_loader import get_key
    return get_key("fred_api_key", "")


# ── FRED 抓取 ──────────────────────────────────────────────────────────────
def _fetch_fred_observations(series_id: str, api_key: str, limit: int) -> Optional[list[tuple[str, float]]]:
    """拉 FRED 序列，返回 [(date, value)] 升序；失败/无数据返回 None。"""
    try:
        r = requests.get(_FRED_OBS_URL, params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }, timeout=_REQUEST_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.warning("FRED %s 请求异常: %s", series_id, e)
        return None
    if r.status_code != 200:
        logger.warning("FRED %s HTTP %s: %s", series_id, r.status_code, r.text[:120])
        return None
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        logger.warning("FRED %s JSON 解析失败", series_id)
        return None

    out: list[tuple[str, float]] = []
    for o in j.get("observations", []):
        v = o.get("value")
        if v in (None, "", "."):  # "." = FRED 缺失值
            continue
        try:
            out.append((o["date"], float(v)))
        except (TypeError, ValueError, KeyError):
            continue
    out.reverse()  # desc → 升序
    return out or None


def _mock_observations(series_id: str, limit: int) -> list[tuple[str, float]]:
    """mock FRED 值（无 key 测试用，确定性构造）。产生示例 regime：杠杆🟡 / 流动性🟡 / 预期🟢。"""
    specs = {
        "T10Y2Y":       {"freq_days": 1, "start": -0.40, "end": -0.08},
        "BAMLH0A0HYM2": {"freq_days": 1, "start": 4.05, "end": 4.21},
        "WALCL":        {"freq_days": 7, "start": 6600000.0, "end": 6600000.0},
        "SOFR":         {"freq_days": 1, "start": 4.33, "end": 4.33},
        "T10YIE":       {"freq_days": 1, "start": 2.20, "end": 2.20},
    }
    spec = specs[series_id]
    today = datetime.now(_TZ_BJ).date()
    out = []
    for i in range(limit):
        frac = i / (limit - 1) if limit > 1 else 0.0
        val = spec["start"] + (spec["end"] - spec["start"]) * frac
        d = today - timedelta(days=spec["freq_days"] * (limit - 1 - i))
        out.append((d.isoformat(), round(val, 6)))
    return out


# ── 趋势 ──────────────────────────────────────────────────────────────────
def _direction(cur: float, base: float, rel_eps: float = 0.005) -> int:
    """cur 相对 base 的方向：+1 升 / -1 降 / 0 平（相对变化超 rel_eps 才判方向）。"""
    if base == 0:
        return 0
    rel = (cur - base) / abs(base)
    if rel > rel_eps:
        return 1
    if rel < -rel_eps:
        return -1
    return 0


# ── 分类器（level + trend 合判）────────────────────────────────────────────
def _classify_t10y2y(vals: list[float]) -> dict:
    cur = vals[-1]
    base = vals[-21] if len(vals) >= 21 else vals[0]
    d = _direction(cur, base)
    was_inverted = base < _T10Y2Y_INVERT
    if cur < _T10Y2Y_INVERT:
        light, trend = "🔴", "deepening_inversion" if d < 0 else "inverted"
        watch = "倒挂中，停卖 put；关注何时回正"
    elif was_inverted:
        # 深度倒挂后回正（dis-inversion）≠ 🟢：历史上是「衰退兑现」警示
        light, trend = "🟡", "rising_from_inversion"
        watch = "深度倒挂后回正，衰退兑现警示，暂不视为完全恢复"
    elif cur > _T10Y2Y_STEEP:
        light, trend = "🟢", "steepening" if d > 0 else "steep"
        watch = f"利差健康；若跌破 {_T10Y2Y_INVERT:.2f} → 🔴"
    else:
        light, trend = "🟡", "flattening" if d < 0 else "flat"
        watch = f"利差平坦；若跌破 {_T10Y2Y_INVERT:.2f} → 🔴"
    return {"value": cur, "light": light, "trend": trend, "watch": watch}


def _classify_hy_oas(vals: list[float]) -> dict:
    cur = vals[-1]
    base = vals[-21] if len(vals) >= 21 else vals[0]
    d = _direction(cur, base)
    trend = "widening" if d > 0 else ("tightening" if d < 0 else "stable")
    if cur < _HYOAS_TIGHT:
        light = "🟢"
    elif cur > _HYOAS_STRESS:
        light = "🔴"
    else:
        light = "🟡"
    watch = (f"若升破 {_HYOAS_STRESS:.2f} → 🔴，停新增 CSP"
             if light != "🔴" else "信用承压，停新增 CSP")
    return {"value": cur, "light": light, "trend": trend, "watch": watch}


def _classify_walcl(vals: list[float]) -> dict:
    cur = vals[-1]
    base4 = vals[-5] if len(vals) >= 5 else vals[0]  # 周频：4 周前
    d4 = _direction(cur, base4, rel_eps=0.001)      # WALCL 变动慢，阈值更小
    # 连续缩表周数（从最新往回数）
    declines = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] < vals[i - 1]:
            declines += 1
        else:
            break
    if declines >= _WALCL_CONSEC:
        light, trend = "🔴", "contracting_4w"
    elif d4 > 0:
        light, trend = "🟢", "expanding"
    elif d4 < 0:
        light, trend = "🟡", "contracting"
    else:
        light, trend = "🟡", "flat"
    watch = f"缩表是否加速（4 周趋势；连续 {declines} 周下降）"
    return {"value": cur, "light": light, "trend": trend, "watch": watch}


def _classify_sofr(vals: list[float]) -> dict:
    cur = vals[-1]
    recent = vals[-21:] if len(vals) >= 21 else vals
    median = statistics.median(recent)  # 近期中位数作为「目标」代理（无 DFF 依赖）
    spike = cur - median
    if spike > _SOFR_SPIKE_BP:
        light, trend = "🔴", "spiking"
    elif spike > 0.05:
        light, trend = "🟡", "rising"
    else:
        light, trend = "🟢", "stable"
    watch = f"若跳升 > 近期中位数+{_SOFR_SPIKE_BP * 100:.0f}bp → 🔴（回购压力）；当前偏离 {spike * 100:+.0f}bp"
    return {"value": cur, "light": light, "trend": trend, "watch": watch}


def _classify_t10yie(vals: list[float]) -> dict:
    cur = vals[-1]
    base = vals[-21] if len(vals) >= 21 else vals[0]
    d = _direction(cur, base)
    trend = "rising" if d > 0 else ("falling" if d < 0 else "stable")
    light = "🟡" if (cur < _T10YIE_LOW or cur > _T10YIE_HIGH) else "🟢"
    watch = f"若 <{_T10YIE_LOW}（通缩）或 >{_T10YIE_HIGH}（通胀恐慌）→ 预期不稳"
    return {"value": cur, "light": light, "trend": trend, "watch": watch}


_CLASSIFIERS = {
    "T10Y2Y": _classify_t10y2y,
    "BAMLH0A0HYM2": _classify_hy_oas,
    "WALCL": _classify_walcl,
    "SOFR": _classify_sofr,
    "T10YIE": _classify_t10yie,
}


# ── 事件日历 ───────────────────────────────────────────────────────────────
def _load_event_calendar(date: str) -> Optional[dict]:
    p = _CONFIG_DIR / "event_calendar.json"
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("event_calendar.json 读取失败: %s", e)
        return None
    try:
        today = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None

    upcoming = []
    for ev in j.get("events", []):
        try:
            d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        days = (d - today).days
        if days >= 0:  # 含当日
            upcoming.append({"type": ev["type"], "date": ev["date"], "days": days})
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: (x["days"], x["date"]))
    nxt = upcoming[0]
    nxt["light"] = ("🟠" if nxt["days"] < _EVENT_NEAR_DAYS
                    else "🟡" if nxt["days"] <= _EVENT_WARN_DAYS else "🟢")
    return nxt


# ── 上下文（VIX / 10Y / 油价，可选）────────────────────────────────────────
def _load_macro_context(date: str) -> Optional[dict]:
    try:
        base = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None
    for i in range(11):
        p = _DATA_DIR / (base - timedelta(days=i)).isoformat() / "macro.json"
        if not p.exists():
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        ctx = {}
        for sym, key in (("^VIX", "VIX"), ("^TNX", "TNX_10y"), ("BZ=F", "Brent")):
            v = j.get(sym)
            if isinstance(v, dict) and isinstance(v.get("price"), (int, float)):
                ctx[key] = v["price"]
        return ctx or None
    return None


# ── regime 合成 ────────────────────────────────────────────────────────────
def _worst(*lights: str) -> str:
    return max(lights, key=lambda l: _SEV.get(l, 0))


def _synthesize(lev: str, liq: str, exp: str, event_light: Optional[str]) -> tuple[str, str]:
    if "🔴" in (lev, liq, exp):
        return "RISK_OFF", "🔴"
    if event_light == "🟠":
        return "EVENT_NEAR", "🟠"
    if lev == "🟢" and liq == "🟢" and exp == "🟢":
        return "STABLE", "🟢"
    return "NEUTRAL", "🟡"


def build_regime(date: str, mock: bool = False) -> Optional[dict]:
    """构造完整 regime dict；无 key 且非 mock 时返回 None（调用方跳过写盘）。"""
    key = "" if mock else _get_fred_key()
    if not key and not mock:
        logger.error("FRED_API_KEY 未配置，跳过（不写盘）。本地测试可用 --mock")
        return None

    forces: dict[str, dict] = {"leverage": {"metrics": {}}, "liquidity": {"metrics": {}}, "expectations": {"metrics": {}}}
    got_any = False

    for series_id, freq, force in _SERIES_SPECS:
        limit = 16 if freq == "weekly" else 40
        obs = _mock_observations(series_id, limit) if mock else _fetch_fred_observations(series_id, key, limit)
        if not obs:
            logger.warning("%s: 无数据（跳过）", series_id)
            continue
        vals = [v for _, v in obs]
        if len(vals) < 3:
            logger.warning("%s: 数据点不足", series_id)
            continue
        got_any = True
        forces[force]["metrics"][series_id] = _CLASSIFIERS[series_id](vals)

    if not got_any:
        logger.error("所有 FRED 序列均无数据，跳过")
        return None

    # 各力 light = 该力最严重指标的灯
    lev_light = _worst(*(m["light"] for m in forces["leverage"]["metrics"].values()))
    liq_light = _worst(*(m["light"] for m in forces["liquidity"]["metrics"].values()))
    exp_light = _worst(*(m["light"] for m in forces["expectations"]["metrics"].values())) or "🟢"
    forces["leverage"]["light"] = lev_light
    forces["liquidity"]["light"] = liq_light
    forces["expectations"]["light"] = exp_light

    nxt = _load_event_calendar(date)
    regime, regime_light = _synthesize(lev_light, liq_light, exp_light, nxt["light"] if nxt else None)

    return {
        "date": date,
        "regime": regime,
        "regime_label": _REGIME_LABEL[regime],
        "regime_light": regime_light,
        "forces": forces,
        "events": {"next": nxt},
        "context": _load_macro_context(date),
        "source": "mock" if mock else "fred",
        "timestamp": datetime.now(_TZ_BJ).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="宏观 regime（杠杆/流动性/预期三力）")
    parser.add_argument("--date", default=None, help="数据日期 YYYY-MM-DD，默认今天(BJT)")
    parser.add_argument("--mock", action="store_true", help="mock FRED 值（无 key 测试框架）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    date = args.date or datetime.now(_TZ_BJ).strftime("%Y-%m-%d")
    data = build_regime(date, mock=args.mock)
    if data is None:
        return 0

    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not args.dry_run:
        out_dir = _DATA_DIR / date
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "macro_regime.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写盘 %s", out_dir / "macro_regime.json")

    nxt = data["events"]["next"]
    ev = f"{nxt['type']} {nxt['date']}（{nxt['days']} 天后 {nxt['light']}）" if nxt else "无已知事件"
    logger.info(
        "regime=%s %s%s · 杠杆 %s · 流动性 %s · 预期 %s · 事件 %s · source=%s",
        data["regime"], data["regime_light"], data["regime_label"],
        data["forces"]["leverage"]["light"], data["forces"]["liquidity"]["light"],
        data["forces"]["expectations"]["light"], ev, data["source"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

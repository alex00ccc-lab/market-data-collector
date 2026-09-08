# -*- coding: utf-8 -*-
"""market_reaction — CPI → Rates → Credit 事件状态机（数据层）。

第一性原理：CPI 数字本身不是风险，危险的是数字出来后债市对 Fed reaction function
的**重新定价**（2Y/10Y 分列重新定价 + 信用利差 velocity）。本脚本把这件事做成
事件锚定的状态机：

    L1 CPI 质量（ex-ante，月频）→ L2 2Y/10Y 确认（ex-post，日频）→ L3 HY OAS 信用确认

产出 data/{date}/event_state.json：8 状态 A-H + risk_signals + 双阶段确认时序。
**只存事实与信号**，不含 CC/CSP 风险预算（那是 holdings-briefing options_income.py 的活）。

关键设计（方案 v1.1.2 锁定）：
  - `event_change_5d = close[T+5] − close[T0]`（event-relative，非 rolling 5D）。
  - 双阶段确认：L2 = T0(initial) → T+1(provisional) → T+5(confirmed)；L3 = T+3~5。
  - A-H 只在 L2/L3 confirmed 后成立；未 confirmed 时 state=UNKNOWN（confirmation 单独表达）。
  - median/trimmed CPI 是 Cleveland Fed **年化率**（FRED units=Percent Change at Annual Rate），
    与 core MoM 不同单位、不可直比，**不 /12**。

数据源：FRED 免费 API（利率/信用/CPI 分项）+ config/event_calendar.json（CPI 发布日）。

Usage:
  python scripts/market_reaction.py --date 2026-09-08
  python scripts/market_reaction.py --mock          # mock FRED 值跑通框架（无 key 测试）
  python scripts/market_reaction.py --dry-run       # 只打印不写盘
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from macro_regime import _fetch_fred_observations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("market_reaction")

_TZ_BJ = timezone(timedelta(hours=8))
_ROOT = Path(__file__).resolve().parent.parent  # market_data/
_DATA_DIR = _ROOT / "data"
_CONFIG_DIR = _ROOT / "config"

# ── FRED 序列定义 ───────────────────────────────────────────────────────────
# 利率（日频，分列水平，用于 divergence） + 盈亏平衡/实际利率（恒等式校验）
_RATE_SERIES = ["DGS2", "DGS10", "DFII10", "T10YIE"]
_HY_OAS_SERIES = "BAMLH0A0HYM2"
# CPI level 序列（Index 1982-84=100）→ MoM 本地算
_CPI_LEVEL = {"CPIAUCSL": "headline", "CPILFESL": "core", "CUSR0000SAH1": "shelter"}
# CPI 年化率序列（Cleveland Fed，FRED 原生 annual rate，非 MoM）
_CPI_ANNUALIZED = {"MEDCPIM158SFRBCLE": "median", "TRMMEANCPIM158SFRBCLE": "trimmed"}

# ── 阈值（tunable，集中定义）───────────────────────────────────────────────
_CORE_COOL = 0.20      # core MoM ≤ 0.20% = 冷
_CORE_HOT = 0.35       # core MoM > 0.35% = 热
_FALSE_COOL_CORE = 0.20  # false-cool 硬规则：core ≤ 此值但 breadth 上行
_HY_STRESS_BP = 0.50   # HY OAS Δ5D ≥ +50bp 且 level > 5.5% → stress（v1 operational threshold）
_HY_WIDEN_BP = 0.20    # Δ5D ≥ +20bp → widening
_HY_STRESS_LEVEL = 5.5  # credit stress 的 level 下界（%）
_DIR_EPS = 1e-9        # 方向判定的纯浮点容差（非 bp 噪声带，只防 == 误判）

# 确认时序（交易日数，event-relative）
_L2_CONFIRM_DAYS = 5   # L2 需 T+5 收盘
_L3_CONFIRM_MIN = 3    # L3 需 T+3~5 收盘


def _get_fred_key() -> str:
    from key_loader import get_key
    return get_key("fred_api_key", "")


# ── mock（无 key 测试，确定性构造）──────────────────────────────────────────
def _mock_observations(series_id: str, limit: int) -> list[tuple[str, float]]:
    """构造一个确定性的「clean disinflation + 2Y↓10Y↓ + HY 稳」场景（state A）。"""
    specs = {
        "DGS2":            {"start": 4.30, "end": 4.00},
        "DGS10":           {"start": 4.60, "end": 4.40},
        "DFII10":          {"start": 2.30, "end": 2.20},
        "T10YIE":          {"start": 2.30, "end": 2.20},
        "BAMLH0A0HYM2":    {"start": 2.70, "end": 2.65},
        "CPIAUCSL":        {"start": 330.0, "end": 332.8},
        "CPILFESL":        {"start": 334.0, "end": 336.8},
        "CUSR0000SAH1":    {"start": 427.0, "end": 429.0},
        "MEDCPIM158SFRBCLE": {"start": 3.40, "end": 3.11},
        "TRMMEANCPIM158SFRBCLE": {"start": 2.90, "end": 2.71},
    }
    spec = specs[series_id]
    freq_days = 30 if series_id in _CPI_LEVEL or series_id in _CPI_ANNUALIZED else 1
    today = datetime.now(_TZ_BJ).date()
    out = []
    for i in range(limit):
        frac = i / (limit - 1) if limit > 1 else 0.0
        val = spec["start"] + (spec["end"] - spec["start"]) * frac
        d = today - timedelta(days=freq_days * (limit - 1 - i))
        out.append((d.isoformat(), round(val, 6)))
    return out


def _fetch(series_id: str, key: str, mock: bool, limit: int) -> list[tuple[str, float]]:
    if mock:
        return _mock_observations(series_id, limit)
    return _fetch_fred_observations(series_id, key, limit) or []


# ── 工具：MoM / delta_1m / 方向 ─────────────────────────────────────────────
def _mom(level_vals: list[float]) -> list[float]:
    """index level → MoM % 序列（当前月相对上月环比 ×100）。"""
    return [(level_vals[i] / level_vals[i - 1] - 1.0) * 100.0
            for i in range(1, len(level_vals))]


def _direction(cur: float, base: float) -> int:
    """cur 相对 base 方向：+1 升 / -1 降 / 0 平（|diff| > _DIR_EPS 才判方向）。"""
    if abs(cur - base) <= _DIR_EPS:
        return 0
    return 1 if cur > base else -1


# ── L1 CPI 质量 ─────────────────────────────────────────────────────────────
def _classify_cpi(level: dict[str, list[float]], annualized: dict[str, list[float]]) -> Optional[dict]:
    """产出 cpi 块：各序列 level + delta_1m + quality_flag。

    level/annnualized 的 value 是升序 float 序列（月频）。各序列需 ≥2 点才有 delta_1m。
    """
    cpi: dict = {}
    ready = True

    for field, vals in level.items():
        if len(vals) < 2:
            ready = False
            cpi[f"{field}_mom"] = None
            cpi[f"{field}_delta_1m"] = None
            continue
        moms = _mom(vals)
        cpi[f"{field}_mom"] = round(moms[-1], 4)
        cpi[f"{field}_delta_1m"] = round(moms[-1] - moms[-2], 4) if len(moms) >= 2 else None

    for field, vals in annualized.items():
        if len(vals) < 2:
            ready = False
            cpi[f"{field}_annualized"] = None
            cpi[f"{field}_delta_1m"] = None
            continue
        cpi[f"{field}_annualized"] = round(vals[-1], 4)
        cpi[f"{field}_delta_1m"] = round(vals[-1] - vals[-2], 4)

    if not ready:
        cpi["quality_flag"] = "pending"
        return cpi

    core_mom = cpi.get("core_mom") or 0.0
    shelter_d1 = cpi.get("shelter_delta_1m") or 0.0
    median_d1 = cpi.get("median_delta_1m") or 0.0

    # 纯规则优先（方案 §12-4）：false-cool 硬规则优先于 level 分类
    if core_mom <= _FALSE_COOL_CORE and shelter_d1 > 0 and median_d1 > 0:
        flag = "false_cool"
    elif core_mom <= _CORE_COOL:
        flag = "cool"
    elif core_mom <= _CORE_HOT:
        flag = "borderline"
    else:
        flag = "hot"

    cpi["quality_flag"] = flag
    return cpi


# ── L2/L3 事件反应 + 确认时序 ───────────────────────────────────────────────
def _latest_cpi_event(today: str) -> Optional[dict]:
    """最近的 CPI 发布日（date ≤ today，取最新）。无则 None。"""
    p = _CONFIG_DIR / "event_calendar.json"
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("event_calendar.json 读取失败: %s", e)
        return None
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    past = []
    for ev in j.get("events", []):
        if ev.get("type") != "CPI":
            continue
        try:
            d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d <= today_d:
            past.append(ev)
    if not past:
        return None
    return max(past, key=lambda e: e["date"])


def _rate_series_block(obs: list[tuple[str, float]], event_date: str) -> dict:
    """单条利率序列的事件反应块：change_1d + event_change_5d。"""
    idx = None
    for i, (d, _) in enumerate(obs):
        if d >= event_date:
            idx = i
            break
    if idx is None:
        return {"change_1d": None, "event_change_5d": None}
    block = {"change_1d": None, "event_change_5d": None}
    if idx + 1 < len(obs):
        block["change_1d"] = round(obs[idx + 1][1] - obs[idx][1], 4)
    if idx + _L2_CONFIRM_DAYS < len(obs):
        block["event_change_5d"] = round(obs[idx + _L2_CONFIRM_DAYS][1] - obs[idx][1], 4)
    return block


def _elapsed_trading_days(obs: list[tuple[str, float]], event_date: str) -> int:
    """自 event_date 起已过去的交易天数（0 = 仅 T0 收盘）。"""
    idx = None
    for i, (d, _) in enumerate(obs):
        if d >= event_date:
            idx = i
            break
    if idx is None:
        return -1  # event_date 尚未出现在序列里（未来事件）
    return len(obs) - 1 - idx


def _classify_credit_stress(hy_vals: list[float]) -> tuple[str, float, Optional[float]]:
    """HY OAS → (credit_stress, level, change_5d)。"""
    level = hy_vals[-1] if hy_vals else 0.0
    change_5d = None
    if len(hy_vals) >= 6:
        change_5d = round(hy_vals[-1] - hy_vals[-6], 4)  # rolling 5D velocity
    if change_5d is not None and change_5d >= _HY_STRESS_BP and level > _HY_STRESS_LEVEL:
        return "stress", round(level, 4), change_5d
    if change_5d is not None and change_5d >= _HY_WIDEN_BP:
        return "widening", round(level, 4), change_5d
    return "normal", round(level, 4), change_5d


def _classify_state(quality: str, d2_dir: int, d10_dir: int, credit_stress: str) -> tuple[str, dict]:
    """8 状态 A-H + risk_signals（纯规则）。d2/d10_dir ∈ {-1,0,+1}。

    语义边界（v14.75 验收拍板）：
      - state(A-H) = Rates/Credit 事件拓扑（2Y/10Y 方向 + HY 信用），**不**强制 CPI 质量门。
        例：borderline CPI + 2Y↓10Y↓ + HY稳 → state=A（降息方向已确认），即便 CPI 非 clean。
      - risk_signals = 策略层消费的权威风险事实（downside_convexity 已把 borderline 记为 mid）。
      - cpi.quality_flag = CPI 独立质量维度，不决定 A-H taxonomy。
      最终风险等级以 risk_signals 为准，不由 state 字母单独推导（state==A ≠ CSP GO）。
    """
    long_end_veto = (d2_dir < 0 and d10_dir > 0)
    if quality in ("hot", "false_cool") or credit_stress in ("widening", "stress"):
        downside = "high"
    elif quality == "borderline":
        downside = "mid"
    else:
        downside = "low"

    sig = {
        "downside_convexity": downside,
        "long_end_veto": long_end_veto,
        "credit_stress": credit_stress,
    }

    # 状态映射（A-H；优先级从上到下）。UNKNOWN 兜底。
    if d2_dir < 0 and credit_stress == "stress":
        state = "H"
    elif quality in ("hot", "false_cool") and d2_dir > 0 and d10_dir > 0 and credit_stress in ("widening", "stress"):
        state = "F"
    elif quality in ("hot", "false_cool") and d2_dir > 0 and d10_dir < 0 and credit_stress == "widening":
        state = "G"
    elif d2_dir > 0 and d10_dir > 0 and credit_stress == "widening":
        state = "E"
    elif long_end_veto and credit_stress == "widening":
        state = "C"
    elif d2_dir > 0 and d10_dir == 0:
        state = "D"
    elif d2_dir < 0 and d10_dir == 0:
        state = "B"
    elif d2_dir < 0 and d10_dir < 0 and credit_stress == "normal":
        state = "A"
    else:
        state = "UNKNOWN"
    return state, sig


# ── 主构造 ─────────────────────────────────────────────────────────────────
def build_event_state(date: str, mock: bool = False) -> Optional[dict]:
    key = "" if mock else _get_fred_key()
    if not key and not mock:
        logger.error("FRED_API_KEY 未配置，跳过（不写盘）。本地测试可用 --mock")
        return None

    event = _latest_cpi_event(date)
    if event is None:
        logger.warning("无历史 CPI 事件（date=%s），无法锚定 event_state", date)
        return None
    event_date = event["date"]

    # ── 利率（40 交易日） ──
    rate_obs: dict[str, list[tuple[str, float]]] = {}
    for sid in _RATE_SERIES:
        obs = _fetch(sid, key, mock, 40)
        if obs:
            rate_obs[sid] = obs
    dgs2 = rate_obs.get("DGS2", [])
    if not dgs2:
        logger.error("DGS2 无数据，跳过")
        return None

    elapsed = _elapsed_trading_days(dgs2, event_date)
    rates: dict = {}
    for sid in _RATE_SERIES:
        rates[sid.lower()] = _rate_series_block(rate_obs.get(sid, []), event_date)

    # divergence_5d（event-relative）
    d2_5 = rates["dgs2"]["event_change_5d"]
    d10_5 = rates["dgs10"]["event_change_5d"]
    divergence_5d = round(d10_5 - d2_5, 4) if (d2_5 is not None and d10_5 is not None) else None
    rates["divergence_5d"] = divergence_5d

    # ── 确认时序 ──
    if elapsed >= _L2_CONFIRM_DAYS:
        l2_value = "confirmed"
    elif elapsed >= 1:
        l2_value = "provisional"
    elif elapsed >= 0:
        l2_value = "initial"
    else:
        l2_value = "pending"  # 未来事件
    l3_value = "confirmed" if elapsed >= _L3_CONFIRM_MIN else "pending"

    # ── L1 CPI ──
    level_vals: dict[str, list[float]] = {}
    for sid, field in _CPI_LEVEL.items():
        obs = _fetch(sid, key, mock, 6)
        level_vals[field] = [v for _, v in obs]
    annualized_vals: dict[str, list[float]] = {}
    for sid, field in _CPI_ANNUALIZED.items():
        obs = _fetch(sid, key, mock, 6)
        annualized_vals[field] = [v for _, v in obs]
    cpi = _classify_cpi(level_vals, annualized_vals)
    quality = cpi.get("quality_flag", "pending") if cpi else "pending"

    # ── L3 信用 ──
    hy_obs = _fetch(_HY_OAS_SERIES, key, mock, 40)
    hy_vals = [v for _, v in hy_obs] if hy_obs else []
    credit_stress, hy_level, hy_change_5d = _classify_credit_stress(hy_vals)

    # ── 方向（confirmed 用 event_change_5d，provisional 用 change_1d）──
    if l2_value == "confirmed":
        d2_dir = _direction(d2_5 or 0.0, 0.0)
        d10_dir = _direction(d10_5 or 0.0, 0.0)
    elif l2_value == "provisional" and rates["dgs2"]["change_1d"] is not None:
        d2_dir = _direction(rates["dgs2"]["change_1d"], 0.0)
        d10_dir = _direction(rates["dgs10"]["change_1d"], 0.0)
    else:
        d2_dir = d10_dir = 0

    # ── 状态（A-H 只在 confirmed；否则 UNKNOWN）──
    if l2_value == "confirmed":
        state, sig = _classify_state(quality, d2_dir, d10_dir, credit_stress)
    else:
        state = "UNKNOWN"
        # provisional 时仍给 early warning（long_end_veto 从 change_1d 预判）
        early_veto = (d2_dir < 0 and d10_dir > 0)
        sig = {
            "downside_convexity": ("high" if quality in ("hot", "false_cool") else
                                   "mid" if quality == "borderline" else "low"),
            "long_end_veto": early_veto,
            "credit_stress": credit_stress,
        }

    # ── 恒等式自检（写入 fact，供 CI 校验）──
    dfii10 = None
    if rate_obs.get("DFII10"):
        dfii10 = rate_obs["DFII10"][-1][1]
    t10yie = None
    if rate_obs.get("T10YIE"):
        t10yie = rate_obs["T10YIE"][-1][1]
    dgs10 = rate_obs["DGS10"][-1][1] if rate_obs.get("DGS10") else None
    identity_ok = None
    if dgs10 is not None and t10yie is not None and dfii10 is not None:
        identity_ok = abs((dgs10 - t10yie) - dfii10) <= 0.05  # ±5bp

    return {
        "date": date,
        "event_type": "CPI",
        "event_date": event_date,
        "elapsed_trading_days": elapsed,
        "state": state,
        "confirmation": {
            "l2_rates": {"value": l2_value, "as_of": date},
            "l3_credit": {"value": l3_value, "as_of": date},
        },
        "cpi": cpi,
        "rates": rates,
        "hy_oas": {"level": hy_level, "change_5d": hy_change_5d},
        "risk_signals": sig,
        "identity_check": {"dgs10_minus_t10yie_equals_dfii10": identity_ok},
        "source": "mock" if mock else "fred",
        "timestamp": datetime.now(_TZ_BJ).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CPI → Rates → Credit 事件状态机（数据层）")
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
    data = build_event_state(date, mock=args.mock)
    if data is None:
        return 0

    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not args.dry_run:
        out_dir = _DATA_DIR / date
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "event_state.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写盘 %s", out_dir / "event_state.json")

    logger.info(
        "state=%s · L2=%s · L3=%s · CPI=%s · credit=%s · divergence_5d=%s · event=%s",
        data["state"], data["confirmation"]["l2_rates"]["value"],
        data["confirmation"]["l3_credit"]["value"], data["cpi"].get("quality_flag"),
        data["risk_signals"]["credit_stress"], data["rates"].get("divergence_5d"),
        data["event_date"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

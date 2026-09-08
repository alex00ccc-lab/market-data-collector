# -*- coding: utf-8 -*-
"""positioning — CFTC SOFR 期货持仓（数据层，独立 state）。

定位「杠杆资金在 SOFR 期货上有多拥挤」，是 ex-ante 的慢变量（周频），与 structural_regime、
event_state 平级、互不嵌套（方案 v1.1.2 三 state 架构）。

只记**原始事实** + 一个机械可复现的 `positioning_state`（拥挤度分位），**不做经济解释**：
「净空 SOFR」不等于「hawkish crowded」——它可能包含 outright rate view / curve positioning /
spread / hedge / basis 等。dove/hawk 的经济解释延后到 v2 methodology。

产出 cache/positioning.json（周频单文件，`as_of` = CFTC report_date 供消费方查陈旧）。

数据源：CFTC Traders in Financial Futures（TFF）报告，Socrata 数据集 `gpe5-46if`，免费免 key。
  - SOFR 的 CFTC 名 = "SECURED OVERNIGHT FINANCING RATE"（cftc_commodity_code 134）。
  - 关键字段：lev_money_positions_long/short（主信号）、asset_mgr / dealer、open_interest_all。

Usage:
  python scripts/positioning.py            # 真实 CFTC 抓取
  python scripts/positioning.py --mock     # mock 值跑通框架
  python scripts/positioning.py --dry-run  # 只打印不写盘
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("positioning")

_TZ_BJ = timezone(timedelta(hours=8))
_ROOT = Path(__file__).resolve().parent.parent  # market_data/
_CACHE_DIR = _ROOT / "cache"

_CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_SOFR_NAME = "SECURED OVERNIGHT FINANCING RATE"
_REQUEST_TIMEOUT = 30
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 拥挤度分位阈值（tunable；leveraged_funds_net 越低越空）
_CROWDED_SHORT_Q = 0.20   # net percentile < 0.20 → crowded_short
_CROWDED_LONG_Q = 0.80    # net percentile > 0.80 → crowded_long


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _net(row: dict, long_key: str, short_key: str) -> Optional[float]:
    """net = long − short（缺失返回 None）。"""
    l, s = _num(row.get(long_key)), _num(row.get(short_key))
    if l is None or s is None:
        return None
    return l - s


def _fetch_cftc(mock: bool = False) -> list[dict]:
    """拉 SOFR TFF 持仓（report_date DESC）。mock 时构造确定性序列。"""
    if mock:
        rows = []
        today = datetime.now(_TZ_BJ).date()
        for i in range(100):
            frac = i / 99.0
            net = round(-1_000_000 - 1_600_000 * frac)  # -1M → -2.6M（当前 crowded_short）
            rows.append({
                "report_date_as_yyyy_mm_dd": (today - timedelta(days=7 * (99 - i))).isoformat(),
                "commodity_name": _SOFR_NAME,
                "lev_money_positions_long": 951_000,
                "lev_money_positions_short": 951_000 - net,
                "asset_mgr_positions_long": 1_111_128,
                "asset_mgr_positions_short": 1_803_718,
                "dealer_positions_long_all": 4_935_955,
                "dealer_positions_short_all": 1_556_637,
                "open_interest_all": 13_075_689,
                "change_in_lev_money_long": -15_344,
                "change_in_lev_money_short": -6_229,
            })
        rows.reverse()  # DESC：rows[0] = 最新报告
        return rows

    params = {
        "$where": f"commodity_name='{_SOFR_NAME}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 200,  # ~4 年周频，够算分位
    }
    url = _CFTC_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _UA)
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        logger.warning("CFTC 请求异常: %s", e)
        return []


def _percentile(values: list[float], target: float) -> float:
    """target 在历史序列中的分位（0~1；越负越贴近 0 = 越空）。"""
    if not values:
        return 0.5
    below = sum(1 for v in values if v <= target)
    return below / len(values)


def build_positioning(mock: bool = False) -> Optional[dict]:
    rows = _fetch_cftc(mock)
    if not rows:
        logger.error("CFTC 无数据，跳过")
        return None

    latest = rows[0]
    report_date = str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10]

    lev_net = _net(latest, "lev_money_positions_long", "lev_money_positions_short")
    dealer_net = _net(latest, "dealer_positions_long_all", "dealer_positions_short_all")
    am_net = _net(latest, "asset_mgr_positions_long", "asset_mgr_positions_short")
    change_1w = _net(latest, "change_in_lev_money_long", "change_in_lev_money_short")

    hist_nets = []
    for row in rows:
        n = _net(row, "lev_money_positions_long", "lev_money_positions_short")
        if n is not None:
            hist_nets.append(n)
    net_percentile = _percentile(sorted(hist_nets), lev_net) if (lev_net is not None and hist_nets) else None

    if net_percentile is None:
        pos_state = "unknown"
    elif net_percentile < _CROWDED_SHORT_Q:
        pos_state = "crowded_short"
    elif net_percentile > _CROWDED_LONG_Q:
        pos_state = "crowded_long"
    else:
        pos_state = "neutral"

    return {
        "instrument": "SECURED OVERNIGHT FINANCING RATE",
        "report": "TFF",
        "dealer_net": dealer_net,
        "asset_manager_net": am_net,
        "leveraged_funds_net": lev_net,
        "net_percentile": round(net_percentile, 4) if net_percentile is not None else None,
        "change_1w": change_1w,
        "open_interest_all": _num(latest.get("open_interest_all")),
        "positioning_state": pos_state,
        "source": "mock" if mock else "cftc",
        "as_of": report_date,
        "timestamp": datetime.now(_TZ_BJ).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CFTC SOFR 期货持仓（数据层，独立 state）")
    parser.add_argument("--mock", action="store_true", help="mock 值（无网测试框架）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    data = build_positioning(mock=args.mock)
    if data is None:
        return 0

    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not args.dry_run:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / "positioning.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写盘 %s", _CACHE_DIR / "positioning.json")

    logger.info(
        "positioning_state=%s · lev_net=%s · percentile=%s · as_of=%s · source=%s",
        data["positioning_state"], data["leveraged_funds_net"],
        data["net_percentile"], data["as_of"], data["source"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

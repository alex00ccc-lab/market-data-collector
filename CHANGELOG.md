# Changelog — market-data-collector

> 扁平列表，每次迭代一条。本文件为 holdings-briefing 子模块的独立 CHANGELOG。
> 格式: `## vN — 日期 — 标题`，附改动文件表、回滚方法。

---

## v7 — 2026-08-16 — 周线 OHLCV 聚合（随 holdings-briefing v14.28 Phase 4 反事实引擎）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.28（Phase 4 反事实损失归因，周线破位规则） |

### 背景

反事实引擎的「周线破位」规则（连续 2 周收盘低于周 MA60）需要周线聚合，但 `indicators.py` 原本只有日线 `calc_*`。新增点状周线聚合（ISO 周、每周最后一个交易日），供 loss_attribution 回溯消费，无 lookahead。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/indicators.py` | **新增** `calc_weekly_ohlc()`（周 OHLCV 聚合）、`weekly_ma_series()`（周简单均线，前 period-1 根返回 0）、周 RSI/MACD/顶底背离标记；纯函数吃 list，不读文件、不写缓存 |

### 验证

- `python -m pytest ../tests/ -q` → 29 passed（含 13 新增反事实测试，其中周线破位路径复用本函数）
- 纯新增，日线 `calc_*` 零行为变化（父项目零行为变化保证）

### 回滚方法

```bash
git revert <v7 commit>
# indicators.py 周线函数为纯新增，删除即回滚；日线消费方不受影响
```

---

## v6 — 2026-08-14 — Twelve Data + finnhub 独立源 key 接入（免费档覆盖实测）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.8（P3/P4 独立源 key + fallback 链） |

### 背景

plan §2.4 目标把 Twelve Data 作为日股/欧股的独立 fallback（`yfinance → twelve_data`）。实测（2026-08-14）发现两个关键事实：

1. **Twelve Data 免费档仅覆盖美股**——东京 (JPX)、港交所 (HKEX)、台湾 (TWSE)、斯德哥尔摩 (XSTO) 均需 Pro/Venture 付费档。因此 Twelve Data 只能作为**美股第三独立源**（800 次/天，远优于 alpha_vantage 的 25 次/天），JP/EU 独立源仍是缺口。
2. **finnhub 免费档不含 `/stock/candle`（历史 OHLCV）**——`/quote` 正常返回（key 有效），但 `/stock/candle` 返回 403 "You don't have access"。故 `FinnhubAdapter.fetch_kline` 在免费档下恒返回 None，仅能降级到 twelvedata/alpha_vantage。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/adapters/twelvedata_adapter.py` | 新增：Twelve Data time_series adapter。`supports_market` 仅 US（诚实反映免费档覆盖）；429→cooldown、401→KeyInvalidError、404/付费档→静默降级；最新在前→归一化为日期升序 |
| `scripts/adapters/finnhub_adapter.py` | docstring 补充免费档限制：`fetch_kline` 免费档恒返回 None（candle 付费），仅 quote/company_profile2/news 可用，key 本身有效 |
| `scripts/source_manager.py` | `_register_defaults()` 注册 `TwelveDataAdapter` |
| `config/sources.json` | 全局 priority 插入 `twelvedata`（finnhub 之后、alpha_vantage 之前）；新增 `twelvedata` adapter 配置，note 标注「免费档 US only，JP/EU 仍是缺口」 |
| `config/keys.yaml` | 填入 `twelvedata_api_key` + `finnhub_api_key`（git-ignored，不入库） |

### 验证

- `TwelveDataAdapter.fetch_kline('NOK', 'US')` → 10 bars 升序，close 10.56 与 indicators 一致 ✅
- US 四只持仓 NOK/TSLA/LRCX/SGOV 全部可抓（含 ETF SGOV）✅
- `FinnhubAdapter` key 有效性：`/quote?symbol=AAPL` 正常返回 ✅；`/stock/candle?symbol=NOK` 返回 403（免费档无 OHLCV）⚠️
- `SourceManager.get_priority('US')` → `[yfinance, yahoo_chart, finnhub, twelvedata, alpha_vantage]` ✅
- `get_priority('JP')` → `[yfinance, yahoo_chart]`（twelvedata 被正确过滤，缺口如实暴露）✅
- `gh secret set TWELVEDATA_API_KEY` + `FINNHUB_API_KEY` 已配置于 `market-data-collector` ✅

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert <v6 commit>
# keys.yaml 为 git-ignored，不影响回滚
```

### 待办（缺口）

- **JP 独立源**：Twelve Data 免费档不含东京；finnhub/alpha_vantage 免费档仅美股。需付费档或另寻免费独立源（用户已接受/暂缓）。
- **finnhub OHLCV**：免费档无 candle，key 仅可用于 fetch_realtime / fetch_fundamentals 扩展（P10 备用方向），不是 OHLCV 逃生舱。

---

## v5 — 2026-08-13 — A股交易日历扩展到 2020–2026（多年度休市日）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.6（cache_status 假阳性告警修复） |

### 背景

`cache_status()` 用「工作日数」估算应有交易日，把 A股节假日误算为「缺失」。A股缓存跨 2020–2026，而 `CN_HOLIDAYS_2026` 只含 2026，导致 513010/588080 误报「缺失 95/105 天」。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/utils/calendar_utils.py` | `CN_HOLIDAYS_2026` → `CN_HOLIDAYS`，补 2020–2025 各年休市日（仅工作日闭市日；周末由 `is_trading_day` 的 weekend 检查覆盖）；`TradingCalendar.__init__` 引用同步更新 |

### 验证

- `--cache-status`：513010 missing 95→3、588080 105→3（均 < 20 告警阈值）✅

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert <v5 commit>
```

---

## v4 — 2026-08-13 — 死代码清理（随 holdings-briefing v14.3）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.3（Phase 3 死代码清理 · M17） |

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/fetch.py` | 删除死函数 `fetch_efinance_realtime`（全仓零调用；efinance 实时行情已由 source_manager / `EFinanceAdapter` 路径替代） |

### 保留说明

`fetch_stooq_history` 未删除 —— 仍被 holdings-briefing `src/report/data_collector.py` 的 backfill 路径（US 回退 / JP 主源）调用。

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert <M17 commit>
```

---

## v3 — 2026-08-09 — CI 告警代码清理（随 holdings-briefing v13）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v13 |

### 改动文件

| 文件 | 改动 |
|------|------|
| `.github/workflows/fetch-daily.yml` | 清理 inline webhook Python 代码，改为结构化调用（待 vendored wechat_alert.py 后切换） |

### 回滚方法

```bash
git revert <commit>
```

---

## v2 — 2026-07-31 — 本地抓取管线 + 币种后缀回退 + CI schedule 禁用

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v9 |
| **涉及仓库** | market-data-collector |

### 改动文件

| Commit | 文件 | 改动 |
|--------|------|------|
| `a080813` | `scripts/fetch_local.py` | 新建 — 本地一体抓取管线 (sync→fetch→indicators→push) |
| `992edcf` | `scripts/fetch_local.py` | 修复 — fetch.py 从 4次/市场 改为 1次全局调用 |
| `a080813` | `scripts/fetch.py` | `CURRENCY_SUFFIX_MAP` + 币种→交易所后缀回退 (SEK→.ST 等) |
| `a080813` | `scripts/adapters/yfinance_adapter.py` | 北欧交易所后缀候选列表 |
| `a080813` | `.github/workflows/fetch-daily.yml` | schedule 禁用，保留 workflow_dispatch |
| `a080813` | `.github/workflows/fetch-weekly.yml` | schedule 禁用，保留 workflow_dispatch |
| `a080813` | `config/holdings.json` | 同步 12 只持仓（含 currency 字段，6981 JP/JPY） |
| `a080813` | `.gitignore` | 新增 `logs/` |

### 关键结果

- 本地抓取恢复：A 股 efinance 2/2 OK（之前 CI 上 0%）
- 数据抓取入口统一：仅 fetch_local.py 写入 data/
- 币种后缀回退就绪：SIVE (SEK→.ST) 等北欧股票可自动适配 yfinance
- CI schedule 禁用：不再重复抓取

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert 992edcf
```

---

## v1 — 2026-07-16 — 多源适配器 + SourceManager + Key 管理

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v7 |
| **涉及仓库** | market-data-collector |

### 改动文件

| Commit | 文件 | 改动 |
|--------|------|------|
| `64c0ecf` | `scripts/key_loader.py` | 新建 — 子模块独立 key_loader，env > keys.yaml 优先级 |
| `972dd65` | `config/keys.yaml` | 新建 — 本地 API key 存储（git-ignored） |
| `7126fed` | `scripts/fetch.py` | efinance 加入优先级，per-market source overrides |
| `a1c67cf` | `scripts/source_manager.py` | 新建 — 多源调度器 + AlphaVantageAdapter + 健康统计 |
| `a1c67cf` | `scripts/adapters/` (5文件) | 新建 — BaseAdapter + YFinance/Stooq/EFinance/AlphaVantage 适配器 |
| `f2d8a4a` | `scripts/fetch.py` | Rate limiter 增加到 1.5s 缓解 yfinance 限流 |
| `c879978` | `scripts/fetch.py` | yfinance 错误分类、5天回退、stooq 市场感知后缀、--lenient 标志 |
| `4be0f40` | `.gitignore` | data/ 加入 gitignore，per_symbol log 记录 quote_date |

### 关键结果

- 数据采集恢复：0/13 → 8/13 (yfinance) → 6/6 US via alpha_vantage
- 多源自动回退：primary → fallback 无人工干预
- 健康统计：per-source 成功率追踪

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert 64c0ecf
```

---

## v0.2 — 2026-07-07 — Stooq 回退 + Obsidian 导出

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v6 |
| **Commit** | `486d518` |

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/fetch.py` | 新增 Stooq CSV 备用源、sources.json 配置优先级、错误回退逻辑 |
| `config/sources.json` | 新建 — 数据源优先级配置，默认 yfinance -> stooq |
| `scripts/export_to_obsidian.py` | 新建 — Obsidian 导出脚本 |
| `config/holdings.json` | 同步本地 holdings 更新 |

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert 486d518
```

---

## v0.1 — 2026-07-03 — JP 市场支持

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v5 |
| **Commit** | `66dd284` |

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/fetch.py` | JP 收盘时间(14:00 BJT)、.T 后缀、JPY、yfinance 抓取 |
| `scripts/utils.py` | TSE 2026 节假日、交易日判断 |
| `config/macro.json` | ^N225 日经225 宏观指标 |
| `config/holdings.json` | 6981.T 村田製作所 |

### 回滚方法

```bash
cd D:\holdings-briefing\market_data && git revert 66dd284
```
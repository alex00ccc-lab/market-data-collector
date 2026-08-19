# Changelog — market-data-collector

> 扁平列表，每次迭代一条。本文件为 holdings-briefing 子模块的独立 CHANGELOG。
> 格式: `## vN — 日期 — 标题`，附改动文件表、回滚方法。

---

## v11 — 2026-08-20 — dividend_yield 单位修复（yfinance 百分比 → fraction）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.49（dividend_yield 单位一致性修复） |

### 背景

父项目 v14.49 修复了 `src/fundamental.py` 的 yfinance `dividendYield` 单位 bug：yfinance 返回**百分比**（KO=2.39、GOOGL=0.26），框架按 **fraction**（`0.02`=2%）读。market_data 子模块里 `yfinance_adapter.py` 与 `fetch.py` 有同款写法（`info.get("dividendYield")` 存原值、未 `/100`），口径不一致，统一修复。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/adapters/yfinance_adapter.py` | `fetch_fundamentals` 的 `dividend_yield` 由存原值改为 `/100`（percent → fraction，None/负数安全） |
| `scripts/fetch.py` | `fetch_yfinance_fundamentals` 同款 `/100` |

### 验证

- 单位换算：`2.39 → 0.0239`、`0.26 → 0.0026`、None → None
- 两文件 `ast.parse` 语法通过

### 回滚方法

```bash
git revert <v11 commit>
# 或恢复 dividend_yield: info.get("dividendYield") 原写法
```

---

## v10 — 2026-08-19 — A/HK 数据源换源：腾讯财经(primary) + mootdx(兜底) + 东财降级

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.41（A/HK 数据源换源） |

### 背景

C1-C7 加固后 A/HK 仍缺价——根因是**东财对大陆住宅 IP 的连接级间歇封锁**（本机在大陆）+ **akshare 未装**。换源根治、不靠换 IP：新增腾讯财经（HTTP、大陆直连、不被封）作 A/HK primary，mootdx（通达信 TCP）作 A股兜底，东财降为最后。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/adapters/tencent_adapter.py` | **新增**：腾讯财经 adapter。`fetch_kline`(fqkline GBK 解码、`qfqday`/`day` 键回退、bar 序 `[date,open,close,high,low,volume]` close 在 index 2)、`fetch_realtime`(qt.gtimg.cn)；代码映射 `sh`/`sz`/`hk`+zfill5；403/429→`RateLimitError` |
| `scripts/adapters/mootdx_adapter.py` | **新增**：通达信 TCP adapter（仅 A股，港股 K 不成熟）。lazy import + `AdapterNotAvailableError`；`bars(frequency=9)` 日线；best-effort 降级（服务器池失效返回 None 不阻塞） |
| `scripts/source_manager.py` | 注册 `TencentAdapter` + `MootdxAdapter` |
| `config/sources.json` | A 链 `tencent→mootdx→efinance→aktools`、HK 链 `tencent→efinance→aktools→yfinance`；新增两条 adapter 配置 |
| `scripts/adapters/__init__.py` | 导出新 adapter |

### 验证

- `fetch.py --lenient --markets A,HK --force` → **6/6 全走 tencent**（513010/588080/588710 + 9992/2631/1888），`_fetch_log` source_health `tencent 100%`、latest 2026-08-18
- 父项目 `pytest tests/test_discipline_conformance.py -q` → 10 passed

### 回滚方法

```bash
git revert <v10 commit>
# 删两个新 adapter + revert source_manager 注册 + sources.json 优先级链 即退「东财-only」旧链
# pip uninstall mootdx 即退依赖
```

### 备注（mootdx 现状）

mootdx（通达信）免费服务器池已大面积失效（38 服务器抽样 5 个仅 1 个 TCP 通且返回空 DataFrame），`bestip` 在 Python 3.14 上因 `asyncio.get_event_loop()` 移除而失败。已接线为 best-effort 兜底，服务器池恢复后自动生效；当前由腾讯独立支撑 A/HK 主链路。

---

## v9 — 2026-08-19 — 数据源加固(C1-C7) + 未来函数修复 + 五问共振重构（随 holdings-briefing v14.38 Phase 1）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.38（投资决策体系修复 Phase 1） |

### 背景

父项目诊断：旧「六指共振」是反向指标（KDJ≡布林、MACD∝−RSI、「超卖=看多」误标为接飞刀）。同时 A/HK/JP 数据源 08-18 全挂（东财 IP 连接级阻断 + akshare 缺失 + HK secid 未补零）。本版修未来函数 + 去共线性 + 五问共振重构 + 数据源加固。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/indicators.py` | **A1**: `calc_support_resistance` 窗口 `closes[i-5:i+6]`(含 5 根未来)→point-in-time；`_weekly_divergence` 只算一次 MACD；新增 `rolling_ma_series` 唯一口径；**B1**: 六指→五问共振（去 KDJ 共线票、修「超卖=看多」误标） |
| `scripts/adapters/efinance_adapter.py` | **C1**: HK secid 补零 `116.{code.zfill(5)}`；`RemoteDisconnected`→`GeoBlockError` |
| `scripts/fetch.py` | **C2**: `_efinance_secid` HK zfill(5)；**C7**: err_msg 追加各源失败原因（限流 vs 断连） |
| `scripts/adapters/aktools_adapter.py` | **C3**: akshare 缺失/过旧抛 `AdapterNotAvailableError`（区分「没装」vs「抓取失败」） |
| `scripts/source_manager.py` | **C6**: 通用异常日志 DEBUG→WARNING；捕获 GeoBlockError/AdapterNotAvailableError；新增 `per_symbol_status`；health_summary 计入 geo_blocked/unavailable |
| `config/sources.json` | **C5**: HK 链追加 `yfinance` 兜底 |

### 验证

- 父项目 `python -m pytest tests/test_discipline_conformance.py -q` → 10 passed
- 五问共振合成测试：CLEAN_UPTREND → mildly_bullish；CLEAN_DOWNTREND → neutral（旧逻辑误标 bullish，接飞刀已修）

### 回滚方法

```bash
git revert <v9 commit>
# 数据源: sources.json HK 链 + secid 补零小改，逐条 revert；akshare 卸载即退
# 五问重构: revert indicators.py 即退六指
```

---

## v8 — 2026-08-17 — calc_vwap（日内/锚定 VWAP，随 holdings-briefing v14.36 双轨报告三指标）

| 属性 | 值 |
|------|-----|
| **父项目** | holdings-briefing v14.36（日报/周报升级：日内 VWAP + 周报 AVWAP） |

### 背景

日报「日内 VWAP」与周报「AVWAP（锚定 VWAP）」需要典型价加权累计，但 `indicators.py` 无 VWAP 函数。新增 `calc_vwap`，纯 list 实现（对齐现有 `calc_ma` 接口，不依赖 pandas/numpy）。

### 改动文件

| 文件 | 改动 |
|------|------|
| `scripts/indicators.py` | **新增** `calc_vwap(highs, lows, closes, volumes, start_idx=0, mode="intraday")`：`typical=(H+L+C)/3`，逐 bar 累计 `Σ(typical×vol)/Σ(vol)`；`mode="intraday"` 单日序列、`mode="anchor"` 从 `start_idx` 起不重置；返回 `round(pv/vol, 4)` |

### 验证

- `python -m pytest ../tests/ -q` → 56 passed（含 `tests/test_stream_b_upgrade.py` 的 `test_calc_vwap_intraday`/`test_calc_vwap_anchor` 已知序列验算）
- 纯新增，日线 `calc_*` 零行为变化

### 回滚方法

```bash
git revert <v8 commit>
# calc_vwap 为纯新增函数，删除即回滚；父项目 obsidian_writer 经 importlib 加载、缺失降级 None（不阻断）
```

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
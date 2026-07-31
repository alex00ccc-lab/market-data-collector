# Changelog — market-data-collector

> 扁平列表，每次迭代一条。本文件为 holdings-briefing 子模块的独立 CHANGELOG。
> 格式: `## vN — 日期 — 标题`，附改动文件表、回滚方法。

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
# a_stock_selector

本项目是一个本地 A 股盘后选股与交易计划建议 MVP，使用 Python + Streamlit + SQLite 实现。

风险提示：本系统交易建议仅基于规则模型生成，不保证盈利或本金安全。请严格控制仓位，独立承担风险。

## MVP 范围

- AKShare 优先的数据源接口，未安装依赖或取数失败时自动使用离线样例数据。
- SQLite 本地存储 A 股基础信息、指数行情、个股日线、行业日线、基础财务数据、行业评分、市场评分、策略结果和运行日志。
- 市场环境评分：指数趋势、赚钱效应、成交活跃度、情绪温度、风格一致性。
- 主线识别只基于行业分类，包含 3 日平滑确认和主线漂移监控。
- P0 基本面硬过滤：ST、退市风险、净利润、扣非净利润、营收同比、资产负债率。
- 个股趋势筛选：米内尔维尼趋势模板。
- 关键点突破：历史新高、250 日新高、120 日平台突破，并记录 `keypoint_date`、`keypoint_price`、`keypoint_type`。
- 突破质量过滤：爆量、冲高回落、炸板弱收盘。
- 输出交易计划建议：建议动作、观察价、触发价、买入区间、止损价、止盈价、移动止盈规则和建议仓位。
- Streamlit 页面：市场总览、主线雷达、个股候选池、观察池、策略运行日志。

## 市场评分口径

市场评分采用 MVP 文档的五模块权重结构：

- 指数趋势
- 赚钱效应
- 成交活跃度
- 情绪温度
- 风格一致性

其中部分子指标使用 `pct_rank` 连续评分近似实现，而不是完全分档评分。这样可以在本地数据和样例数据上保持评分连续、可排序，并便于后续回测校准。

## 快速开始

```powershell
uv sync
uv run a-stock-selector init
uv run a-stock-selector run
uv run streamlit run app.py
```

默认数据库在 `data/a_stock_selector.sqlite3`。

## 命令

```powershell
uv run a-stock-selector init          # 建库并写入样例数据
uv run a-stock-selector fetch         # 尝试 Tushare、AKShare，失败后回退样例数据
uv run a-stock-selector run           # 运行策略并写入 strategy_result
uv run a-stock-selector run --refresh # 先刷新数据再运行策略
```

## Tushare Token

真实数据建议优先使用 Tushare。不要把 token 写进代码，复制 `.env.example` 为 `.env` 后填写：

```powershell
Copy-Item .env.example .env
notepad .env
```

或在当前终端设置环境变量：

```powershell
$env:TUSHARE_TOKEN="你的token"
$env:TUSHARE_MAX_STOCKS="0"
$env:TUSHARE_LOOKBACK_DAYS="430"
$env:TUSHARE_FETCH_TURNOVER="0"
uv run a-stock-selector fetch
```

数据源优先级为 `Tushare -> AKShare -> Sample`。如果没有配置 token，程序会自动跳过 Tushare。

`TUSHARE_MAX_STOCKS` 控制扫描股票数量。设为 0 表示全市场扫描；程序会按交易日批量拉取行情，并按报告期批量拉取财务指标。`TUSHARE_LOOKBACK_DAYS` 默认 430，用于满足 250 日新高和米内尔维尼趋势模板所需历史长度。`TUSHARE_FETCH_TURNOVER=0` 会跳过全市场换手率取数以显著提速；行情金额、成交量和指数金额仍会用于活跃度与突破质量判断。

## 说明

第一阶段不做自动交易、不做实时盘口、不做概念板块主线识别。所有结果仅作为规则模型输出的观察清单和交易计划草案。

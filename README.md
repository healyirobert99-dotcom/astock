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
uv run a-stock-selector fetch         # 使用免费数据源刷新全市场数据
uv run a-stock-selector run           # 运行策略并写入 strategy_result
uv run a-stock-selector run --refresh # 先刷新数据再运行策略
```

## 免费数据源

当前默认真实数据工作流不再依赖 Tushare，优先使用 AKShare 聚合的免费接口：

- 新浪全市场行情：用于快速补充最新交易日全市场日线。
- 腾讯指数/个股日线：用于核心指数和个股历史补充。
- 东方财富接口：作为个股历史行情兜底。

如果本地库已有 `stock_basic`、`financials`、历史 `stock_daily`，刷新时会优先复用本地股票基础信息和财务数据，只追加最新行情，避免再次卡在 Tushare 财务分块接口。

复制 `.env.example` 为 `.env` 后可按需调整免费源扫描参数：

```powershell
Copy-Item .env.example .env
notepad .env
```

或在当前终端设置环境变量：

```powershell
$env:FREE_MAX_STOCKS="0"
$env:FREE_LOOKBACK_DAYS="430"
uv run a-stock-selector fetch
```

默认数据源优先级为 `AKShare 免费源 -> Sample`。Tushare 相关代码保留为可选兼容实现，但不再作为默认刷新路径。

`FREE_MAX_STOCKS` 控制扫描股票数量，设为 0 表示全市场扫描；`FREE_LOOKBACK_DAYS` 默认 430，用于满足 250 日新高和米内尔维尼趋势模板所需历史长度。由于免费源财务数据覆盖不稳定，当前刷新会优先复用本地最近财报数据；财务缺失会在基本面状态和剔除原因中提示。

## 说明

第一阶段不做自动交易、不做实时盘口、不做概念板块主线识别。所有结果仅作为规则模型输出的观察清单和交易计划草案。

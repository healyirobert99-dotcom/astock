# a_stock_selector 项目交接文档

本文档用于后续在 Cloud Code 中继续开发当前项目。文档记录截至当前版本的已开发内容、规则口径、数据库结构、运行方式、验证方式、注意事项和后续待办。

## 1. 项目概览

项目名称：`a_stock_selector`

项目定位：本地 A 股盘后选股与交易计划建议工具。

技术栈：

- Python 3.11+
- Streamlit
- SQLite
- Pandas / NumPy
- Altair
- Tushare
- AKShare 作为可替换数据源边界

当前阶段：MVP 候选验收版，已进入观察型试运行阶段。

明确不做的内容：

- 不做自动交易
- 不做实时盘口
- 不做概念板块主线识别
- 不做新闻、公告、研报、AI 题材逻辑识别
- 不输出投资承诺或收益保证

风险提示文案：

> 本系统交易建议仅基于规则模型生成，不保证盈利或本金安全。请严格控制仓位，独立承担风险。

## 2. 当前目录结构

```text
C:\Users\wanyu\Documents\量化选股
├─ a_stock_selector/
│  ├─ app.py              # Streamlit 前端
│  ├─ cli.py              # 命令行入口
│  ├─ config.py           # 策略参数与项目路径
│  ├─ data_provider.py    # Sample / AKShare / Tushare 数据源与入库
│  ├─ db.py               # SQLite schema、连接、迁移、写入工具
│  ├─ indicators.py       # 均线、分位打分、最新交易日工具
│  └─ strategy.py         # 市场评分、主线评分、筛选、交易计划
├─ tests/
│  └─ test_strategy.py
├─ app.py                 # Streamlit 入口，导入 a_stock_selector.app.main
├─ pyproject.toml
├─ README.md              # 当前运行与交付说明
├─ PROJECT_HANDOFF.md     # 本文档
├─ .env.example
├─ .gitignore
├─ .streamlit/
├─ data/                  # 本地 SQLite 数据目录，不应打包提交
└─ deliverables/          # 之前生成的交付物目录
```

## 3. 环境与敏感信息

`.env` 已用于保存 Tushare token。不要把 `.env`、真实 token、SQLite 数据库、缓存目录打包或提交。

`.env.example` 只保留占位配置。

典型 `.env`：

```text
TUSHARE_TOKEN=replace_with_your_tushare_token
TUSHARE_MAX_STOCKS=0
TUSHARE_LOOKBACK_DAYS=430
TUSHARE_FETCH_TURNOVER=0
```

注意：

- `TUSHARE_MAX_STOCKS=0` 表示全市场扫描。
- `TUSHARE_LOOKBACK_DAYS=430` 用于满足 250 日新高和趋势模板历史长度。
- `TUSHARE_FETCH_TURNOVER=0` 会跳过全市场换手率抓取以提速；此时换手率过滤只在数据非零时生效。
- 不要在代码、文档、截图、zip 中暴露真实 token。

## 4. 运行方式

推荐在项目根目录执行：

```powershell
cd "C:\Users\wanyu\Documents\量化选股"
```

安装依赖：

```powershell
uv sync
```

初始化样例数据：

```powershell
uv run a-stock-selector init
```

刷新 Tushare 全市场数据：

```powershell
uv run a-stock-selector fetch
```

运行策略：

```powershell
uv run a-stock-selector run
```

刷新数据后运行策略：

```powershell
uv run a-stock-selector run --refresh
```

启动前端：

```powershell
uv run streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

如果 `uv run` 找不到命令，可使用本机 Python：

```powershell
C:\Users\wanyu\AppData\Local\Programs\Python\Python312\python.exe -m a_stock_selector.cli run
C:\Users\wanyu\AppData\Local\Programs\Python\Python312\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

当前本地前端地址：

```text
http://127.0.0.1:8501
```

## 5. 当前已开发功能

### 5.1 数据源

已实现：

- `SampleDataProvider`
- `AKShareDataProvider`
- `TushareDataProvider`
- `HybridDataProvider`

当前默认真实数据工作流使用 Tushare。

Tushare 数据包括：

- A 股基础列表
- 指数日线
- 个股日线
- 行业日线聚合
- 基础财务指标

Tushare 指数优先级已补入：

- 沪深300：`000300`
- 中证500：`000905`
- 中证1000：`000852`
- 国证2000：`399303`
- 创业板指：`399006`

仍保留旧指数：

- 上证指数：`000001`
- 深证成指：`399001`

### 5.2 SQLite 存储

默认数据库：

```text
data/a_stock_selector.sqlite3
```

主要表：

- `stock_basic`
- `index_daily`
- `stock_daily`
- `industry_daily`
- `financials`
- `data_snapshot`
- `market_score`
- `industry_score`
- `strategy_result`
- `watch_pool`
- `run_log`

`init_db()` 会自动执行 schema 初始化和轻量迁移。

### 5.3 前端页面

Streamlit 页面包括：

- 市场总览
- 主线雷达
- 个股候选池
- 观察池
- 策略运行日志

页面显著位置展示风险提示。

已修复过的问题：

- Streamlit 顶部栏遮挡页面
- 行业明细表 HTML 泄露
- 页面卡片和空白条异常
- 数据刷新/策略运行进度条
- 候选为空时增加剔除原因漏斗和预备观察说明

注意：前端仍是 MVP 展示，不是最终 UI。

## 6. 规则配置

当前 `a_stock_selector/config.py` 中 `StrategyConfig` 已按规则一致性审核口径重写。

核心参数：

```python
MARKET_MIN_SCORE = 50
MARKET_TRADE_SCORE = 65

MAINLINE_CANDIDATE_SCORE = 70
MAINLINE_CONFIRMED_SCORE = 80
MAINLINE_CONFIRM_DAYS = 3
MAINLINE_DOWNGRADE_DAYS = 2
MAINLINE_REMOVE_DAYS = 3

MIN_AVG_AMOUNT_20D = 100_000_000
MIN_LIST_DAYS = 250
MIN_PRICE = 3
MIN_TURNOVER_20D = 1.0

MAX_DEBT_RATIO = 75
MIN_REVENUE_YOY = -10

VOLUME_BREAKOUT_MIN_RATIO_5D = 1.5
VOLUME_BREAKOUT_MIN_RATIO_20D = 1.3
VOLUME_BREAKOUT_MAX_RATIO_20D = 2.5

CLOSE_HIGH_MIN_RATIO = 0.98
CLOSE_MA20_MAX_RATIO = 1.20
LEADER_CLOSE_MA20_MAX_RATIO = 1.15

STOP_LOSS_FALLBACK = 0.08
MIN_HISTORY_DAYS = 260
```

注意：原来的 `mainline_score_threshold = 60` 已不再作为主线确认标准。

## 7. 数据库字段现状

### 7.1 stock_basic

字段包括：

- `code`
- `name`
- `industry`
- `list_date`
- `is_st`
- `is_delist_risk`
- `is_suspended`

### 7.2 stock_daily

字段包括：

- `code`
- `trade_date`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `amount`
- `pct_chg`
- `turnover_rate`
- `is_suspended`
- `is_limit_up`
- `is_limit_down`

### 7.3 industry_score

新增和关键字段：

- `base_score`
- `momentum_score`
- `breadth_score`
- `amount_score`
- `persistence_score`
- `strength_score`
- `width_score`
- `capacity_score`
- `leader_score`
- `logic_score`
- `confirmed`
- `is_candidate_mainline`
- `stability_days`
- `rank_change`
- `score_change`
- `drift_flag`
- `drift_status`

### 7.4 strategy_result

已保留旧字段兼容前端，同时新增最终口径字段。

新增和关键字段：

- `signal_status`
- `fundamental_status`
- `trend_template_type`
- `volume_quality`
- `close_quality`
- `risk_level`
- `trade_plan_type`
- `suggested_action`
- `watch_price`
- `trigger_price`
- `buy_lower`
- `buy_upper`
- `suggested_buy_price`
- `stop_loss_price`
- `take_profit_1`
- `take_profit_2`
- `trailing_stop_rule`
- `suggested_position`
- `risk_warning`

旧兼容字段：

- `action`
- `buy_range_low`
- `buy_range_high`
- `stop_loss`
- `take_profit`
- `moving_take_profit_rule`
- `position_pct`

后续开发应优先使用新字段，旧字段只作兼容。

### 7.5 watch_pool

已同步增加交易计划字段：

- `trade_plan_type`
- `suggested_action`
- `watch_price`
- `trigger_price`
- `buy_lower`
- `buy_upper`
- `suggested_buy_price`
- `stop_loss_price`
- `take_profit_1`
- `take_profit_2`
- `trailing_stop_rule`
- `suggested_position`
- `risk_warning`

## 8. 市场环境评分

实现位置：

```text
a_stock_selector/strategy.py::score_market
```

当前总分结构：

- 指数趋势：30 分
- 赚钱效应：30 分
- 成交活跃度：15 分
- 情绪温度：15 分
- 风格一致性：10 分

说明：市场评分采用 MVP 文档要求的五模块权重结构；部分子项为了在本地数据条件下保持连续、可排序和便于回测，使用 `pct_rank` 连续评分近似实现，而不是完全按离散档位打分。

指数趋势优先使用：

- 沪深300
- 中证500
- 中证1000
- 国证2000
- 创业板指

如果这些指数没有被抓到，会 fallback 到数据库中已有指数。

赚钱效应使用：

- 上涨家数
- 下跌家数
- 上涨家数占比
- 上涨家数 / 下跌家数
- 涨幅大于 5% 家数
- 跌幅大于 5% 家数
- 涨停家数
- 跌停家数

风险级别：

- `>= 75`：积极
- `>= 65`：可交易
- `>= 50`：观察
- `< 50`：谨慎

## 9. 主线评分

实现位置：

```text
a_stock_selector/strategy.py::score_industries
```

行业主线评分拆分：

- 持续性：25
- 强度：20
- 宽度：15
- 资金容量：15
- 龙头结构：15
- 逻辑支撑增强：10

当前 `logic_score` 仍是价格强度代理分，不做概念、新闻、公告逻辑识别。

主线确认规则：

候选主线：

- 连续 3 日 `base_score >= 70`
- 或最近 5 日中至少 3 日 `base_score >= 70`

市场主线：

- 连续 3 日 `base_score >= 80`
- 或最近 5 日中至少 3 日 `base_score >= 80`

当前字段：

- `is_candidate_mainline`
- `confirmed`
- `stability_days`

## 10. 主线漂移

实现位置：

```text
a_stock_selector/strategy.py::add_mainline_state
```

规则：

- 昨日主线排名前 3
- 今日跌出前 10
- 得分下降超过 20 分
- 则 `drift_flag = 1`

记录字段：

- `stability_days`
- `rank_change`
- `score_change`
- `drift_flag`
- `drift_status`

## 11. 基本面硬过滤

实现位置：

```text
a_stock_selector/strategy.py::_fundamental_exclude_reason
```

P0 硬过滤：

- 非 ST
- 非 *ST
- 非退市风险
- 非停牌
- 最近一年净利润 > 0
- 最近一年扣非净利润 > 0
- 营收同比增长 > -10%
- 资产负债率 < 75%
- 上市超过 250 个交易日
- 价格 >= 3
- 20 日平均成交额 >= 1 亿
- 20 日平均换手率 >= 1.0，仅当换手率数据非零时启用

重要修复：

- 不再要求 `revenue_yoy > 0`
- 当前规则为 `revenue_yoy > -10%`

注意：

- Tushare 财务数据目前用 `fina_indicator` 的 `profit_dedt` 作为净利润和扣非净利润代理字段。
- 这是因为批量 `income` 接口在当前环境不稳定或返回不完整。
- 后续如 Tushare 权限允许，应改为更准确的净利润字段来源。

## 12. 趋势模板 A/B/C

实现位置：

```text
a_stock_selector/strategy.py::_trend_template_type
```

A 类趋势股：

- Close > MA50
- Close > MA150
- Close > MA200
- MA50 > MA150
- MA150 > MA200
- MA200 当前值 > MA200 20 日前
- Close / 250 日最高价 >= 0.75
- Close / 250 日最低价 >= 1.30

B 类趋势股：

- Close > MA50
- Close > MA200
- MA50 > MA200
- Close / 250 日最高价 >= 0.75
- 允许 MA150 > MA200 或 MA200 上行条件略弱

C 类趋势股：

- 不进入默认候选池

输出字段：

- `trend_template_type`

## 13. 关键点突破

实现位置：

```text
a_stock_selector/strategy.py::_detect_keypoint
```

关键点类型：

- 收盘价创历史新高
- 收盘价创 250 日新高
- 收盘价突破 120 日平台高点

同时满足：

- `VOL > VOL_MA5 * 1.5`
- `VOL > VOL_MA20 * 1.3`
- `VOL < VOL_MA20 * 2.5`
- 收盘位置强度 > 0.7
- `Close / MA20 < 1.20`
- 龙头 A 类使用更严格 `Close / MA20 < 1.15`
- `Close / High >= 0.98`

输出字段：

- `volume_quality`
- `close_quality`
- `keypoint_date`
- `keypoint_price`
- `keypoint_type`

## 14. 交易计划

实现位置：

```text
a_stock_selector/strategy.py::_build_trade_plan
```

计划类型：

1. 龙头突破试错计划
2. 中军回踩确认计划

输出字段：

- `trade_plan_type`
- `suggested_action`
- `watch_price`
- `trigger_price`
- `buy_lower`
- `buy_upper`
- `suggested_buy_price`
- `stop_loss_price`
- `take_profit_1`
- `take_profit_2`
- `trailing_stop_rule`
- `suggested_position`
- `risk_warning`

市场评分低于 65 时：

- `signal_status = 观察` 或 `重点跟踪`
- `suggested_action = 仅观察`
- `suggested_position = 0`
- `buy_lower = NULL`
- `buy_upper = NULL`
- `suggested_buy_price = NULL`
- `take_profit_1 = NULL`
- `take_profit_2 = NULL`
- 不输出正式买入区间

这是重要语义修复，避免市场弱时页面展示买入区间造成误导。

## 15. 当前真实数据状态

最后一次已验证策略批次：

```text
batch_id = 26e46876bd54
trade_date = 2026-05-20
candidate_count = 0
excluded_count = 5519
```

市场评分：

```text
index_trend_score = 30.0
profit_effect_score = 3.77
activity_score = 6.57
sentiment_score = 1.51
style_consistency_score = 8.49
total_score = 50.34
risk_level = 观察
```

最新行业前列：

```text
半导体    base_score 84.72  confirmed 0  stability_days 1
红黄酒    base_score 72.17  confirmed 0
软饮料    base_score 63.61  confirmed 0
电器仪表  base_score 61.53  confirmed 0
```

当前候选为 0 的主要原因：

- 半导体虽然当日分数高，但只稳定 1 日
- 尚未满足连续 3 日 `base_score >= 80` 或最近 5 日至少 3 日 `>= 80`
- 新规则下这属于正常结果，不是运行失败

低市场分交易计划验证：

```text
market_score < 65 时正式买入区间违规数 = 0
```

## 16. 已修复过的重要问题

### 16.1 Tushare 财务字段错误

早期版本使用了不存在或不稳定字段，导致所有财务指标为 0 或默认值。

当前改为：

- `profit_dedt` 作为净利润/扣非净利润代理
- `q_sales_yoy` 作为营收同比
- `debt_to_assets` 作为资产负债率

历史空财务行已经清理，后续入库会删除 `report_date` 为空的行。

### 16.2 全市场扫描速度

已优化：

- 按交易日批量抓取全市场日线
- 增量刷新跳过已缓存交易日
- 财务指标按报告期和代码块批量抓取
- 可通过 `TUSHARE_FETCH_TURNOVER=0` 提速

### 16.3 行业日线断裂

早期增量刷新会把新增行业日线从 1000 重新起算，导致行业指数断裂，主线排序错误，例如白酒错误排第一。

当前修复：

- 增量写入个股日线后，用完整个股日线重建 `industry_daily`
- 当前行业主线不再受该断裂影响

### 16.4 市场分低仍输出买入区间

已修复：

- `market_score < 65` 时不输出正式买入区间和止盈价
- `suggested_position = 0`
- `suggested_action = 仅观察`

## 17. 验证方式

编译检查：

```powershell
C:\Users\wanyu\AppData\Local\Programs\Python\Python312\python.exe -m py_compile a_stock_selector/config.py a_stock_selector/db.py a_stock_selector/data_provider.py a_stock_selector/strategy.py a_stock_selector/app.py tests/test_strategy.py
```

测试：

```powershell
C:\Users\wanyu\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q
```

当前结果：

```text
1 passed
```

运行真实策略：

```powershell
C:\Users\wanyu\AppData\Local\Programs\Python\Python312\python.exe -m a_stock_selector.cli run
```

验证低市场分不输出买入区间：

```sql
SELECT count(*)
FROM strategy_result
WHERE batch_id = (SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1)
  AND market_score < 65
  AND status = 'included'
  AND (
    buy_lower IS NOT NULL
    OR buy_upper IS NOT NULL
    OR suggested_buy_price IS NOT NULL
    OR take_profit_1 IS NOT NULL
    OR take_profit_2 IS NOT NULL
    OR suggested_position <> 0
  );
```

期望结果：

```text
0
```

## 18. 数据库迁移说明

当前迁移是自动的。

触发方式：

- 启动 Streamlit 时调用 `init_db(DEFAULT_DB_PATH)`
- CLI 运行时调用 `init_db(args.db)`
- 测试创建临时库时调用 `init_db(db_path)`

迁移函数：

```text
a_stock_selector/db.py::migrate_schema
```

迁移方式：

- 使用 `ALTER TABLE ADD COLUMN`
- 不删除旧字段
- 不重建旧表

注意：

- SQLite 不会自动删除旧列，因此旧交易计划字段仍在。
- 后续如果要完全清理旧字段，需要新建表迁移数据，不建议在 MVP 阶段做。

## 19. Cloud Code 后续开发建议

优先级最高：

1. 保持当前规则口径，不要再引入旧的 `mainline_score_threshold=60`。
2. 前端所有交易计划展示改用新字段，不要使用旧的 `buy_range_low/buy_range_high/stop_loss/take_profit`。
3. 针对 `market_score < 65` 的展示，明确标注“仅观察”，不要画出买入区间。
4. 增加更细的筛选漏斗展示：基本面、趋势、关键点、主线确认、市场分。
5. 检查 Tushare 指数是否成功抓到沪深300、中证500、中证1000、国证2000、创业板指。

可以后续优化但不要混入规则一致性修复：

- 更漂亮的 UI
- 更多图表
- 导出 Excel
- 观察池手动备注
- 自动生成报告

仍未实现：

- 概念板块主线识别
- 题材逻辑强度识别
- 新闻/公告/研报数据
- 真实停牌接口
- 更准确的年度净利润字段来源
- 自动交易
- 实时盘口

## 20. 打包和交付注意事项

不要打包：

- `.env`
- `data/`
- `.pytest_cache/`
- `__pycache__/`
- `.venv/`
- 任何包含 token 的截图或日志

之前生成过交付物目录：

```text
deliverables/
├─ a_stock_selector_project.zip
├─ candidates_latest.csv
└─ market_overview.png
```

当前交付物应在交付前重新生成，并继续排除 `.env`、SQLite 数据库、缓存目录和 `*.pyc` 文件。

## 21. 常用只读诊断 SQL

查看最新批次：

```sql
SELECT *
FROM run_log
ORDER BY run_timestamp DESC
LIMIT 5;
```

查看市场评分：

```sql
SELECT *
FROM market_score
WHERE batch_id = (
  SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1
);
```

查看行业前 20：

```sql
SELECT industry, rank, base_score, confirmed, is_candidate_mainline,
       stability_days, rank_change, score_change, drift_flag
FROM industry_score
WHERE batch_id = (
  SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1
)
ORDER BY rank
LIMIT 20;
```

查看候选：

```sql
SELECT code, name, industry, signal_status, trend_template_type,
       trade_plan_type, suggested_action, suggested_position,
       buy_lower, buy_upper, suggested_buy_price
FROM strategy_result
WHERE batch_id = (
  SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1
)
AND status = 'included'
ORDER BY industry_score DESC, code;
```

查看主要剔除原因：

```sql
SELECT exclude_reason, COUNT(*) AS count
FROM strategy_result
WHERE batch_id = (
  SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1
)
AND status = 'excluded'
GROUP BY exclude_reason
ORDER BY count DESC
LIMIT 20;
```

## 22. 第二轮规则一致性修复 (2026-05-20)

### 22.1 交易计划修复
- 龙头突破试错：buy_lower=keypoint_price, buy_upper=keypoint_price*1.02
- 龙头止损：三候选值 max逻辑 + MA10高于买入价时忽略保护
- 龙头止盈：take_profit_1=1.10, take_profit_2=1.20
- 中军回踩确认：实现 true pullback detection (breakout→1-5日缩量回踩→confirm放量收阳)
- 中军买入区间：buy_lower=max(MA20, keypoint*0.98), buy_upper=min(confirm_close*1.02, keypoint*1.03)
- 中军止损：max(pullback_low, MA20, keypoint*0.97)
- 中军止盈：1.12/1.25

### 22.2 keypoint_price 修复
- 历史新高：keypoint_price=previous high.max()
- 250日新高：keypoint_price=previous high.tail(250).max()
- 120日平台：keypoint_price=platform_high
- 新增 breakout_close, breakout_day_low, breakout_ma10 字段

### 22.3 主线评分修复
- base_score 不含 logic_score (仅含 persistence+strength+width+capacity+hot)
- total_score = base_score + logic_score
- 主线确认/候选/漂移全部使用 base_score
- leader_score → hot_score（占位；真正leader_score需按个股维度补齐）

### 22.4 市场风格一致性
- 改为：沪深300/500/1000中≥2个>MA20=5分；涨跌比与指数状态一致=5分

### 22.5 fundamental_status 分级
- A/B/C/D 四档，MVP默认只允许 A/B 进入候选池

### 22.6 CSV 导出完整字段 (29列)
- code/name/industry/status/signal_status/market_score/industry_score/fundamental_status/trend_template_type/keypoint_type/keypoint_price/volume_quality/close_quality/risk_level/trade_plan_type/suggested_action/watch_price/trigger_price/buy_lower/buy_upper/suggested_buy_price/stop_loss_price/take_profit_1/take_profit_2/trailing_stop_rule/suggested_position/include_reason/exclude_reason/risk_warning

### 22.7 数据库迁移 (第二轮)
- industry_score: 新增 total_score, hot_score
- run_log: 5个漏斗列 (之前已加)

## 23. 当前接手结论

项目已通过第二轮规则一致性修复，核心策略逻辑与MVP文档严格对齐。

当前最重要的事实：

- 交易计划精确匹配 MVP 公式（龙头+中军双轨）
- keypoint_price 改用突破位而非收盘价
- base_score 与 total_score 分离
- 市场风格一致性按指数均线逻辑重算
- fundamental_status A/B/C/D 分级
- 中军回踩确认实现真实 pullback detection
- 待补齐：real leader_score（需个股维度 250d/60d 新高 + 趋势中军检测）

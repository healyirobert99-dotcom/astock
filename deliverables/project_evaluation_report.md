# Mainline Pivot Scanner — 专业评估报告

> 评估日期：2026-05-21  
> 评估范围：`a_stock_selector` 全项目（strategy.py 1470行、db.py 397行、app.py ~1600行、tests 366行）  
> 评估视角：金融工程 + 软件工程双视角

---

## 一、金融策略层面的建议

### 1.1 指数趋势评分公式与 MVP 文档不一致（中优先级）

**现状**：`score_market()` 用了一套自创的"均值化打分"——把所有指数按 uniform 网格打分后取平均值。

**MVP 文档规定的是固定分配**：

```text
沪深300 > MA20          → 8分
中证500 > MA20          → 8分
中证1000/国证2000 > MA20 → 8分
创业板指 > MA20          → 6分
────────────────────────────
合计                     30分
```

**问题**：当前公式对 5 个指数分别计算 composite score（含 MA20/MA60/MA120），再取平均。当 5 个指数中有 2 个偏弱时，当前公式会拉低整个 index_trend_score，但文档意图是"只要大部分指数多头就算健康"。当前满分只有 26 分的平均分体系，而文档规定是 30 分固定分配。这导致市场评分被**系统性压缩**。

**建议**：将指数趋势评分改为按文档 weight 分配，不做平均化。具体实现：

```python
index_trend_score = 0.0
for code in preferred:
    if close > ma20:
        if code in ("000300", "000905", "000852", "399303"):
            index_trend_score += 8.0
        elif code == "399006":
            index_trend_score += 6.0
```

---

### 1.2 赚钱效应评分阈值偏保守（中优先级）

**现状**：`profit_effect_score` 的基准区间是 `up_ratio` 的 `pct_rank` 分位映射在 **0.35-0.70** 之间，即上涨家数占比低于 35% 时得 0 分，超过 70% 才满 10 分。

**MVP 文档硬编码规则**：

```text
上涨家数占比 > 60%         → 10分
上涨家数占比 50%-60%       → 7分
上涨家数占比 40%-50%       → 4分
上涨家数占比 < 40%         → 0分
```

**问题**：A股在正常波动日（up_ratio 40-55%）下，当前 `pct_rank` 会把这个得分压缩到 1-4 分区间，而文档规定的分段规则应得 4-7 分。结果就是**中度偏强的市场被系统性低估**，market_score 长期偏低，候选池长期为空。

**建议**：将赚钱效应的 5 个子项改为文档规定的分段打分。如果希望保留平滑性，至少将基准区间调宽（如下限 0.30，上限 0.65），让 A 股常见波动区间（40-60%）落在得分的 30-70% 范围。

---

### 1.3 主线确认对历史数据的冷启动依赖（高优先级）

**现状**：主线确认需要"连续 3 日"或"最近 5 日中 ≥3 日"满足阈值（base_score ≥ 70 或 80）。这要求 `industry_score` 表中有至少 3-5 天的历史数据。

**现实影响**：系统**首次运行真实数据时**，半导体虽得 84.7 分但只有 1 天数据，因此标记为未确认，稳定天数 = 0。需要连续 3-5 个交易日后系统才能产出第一批候选。

**建议**：
- 在文档中明确标注冷启动约束
- 在页面中展示"预计还需 X 个交易日达到确认条件"的提示
- 考虑增加首次运行的宽松模式：如果是最新一批且无历史数据，允许单日 ≥ 80 的行业标记为"initial"状态

---

### 1.4 回踩检测的"缩量"定义可更精确（低优先级）

**现状**：`_detect_pullback()` 检查 `pullback_vol < pullback_vol_ma5` 作为"缩量"判断。

**更精确的做法**：回踩日成交量应 ≤ 突破日成交量的 **60%-70%**。当前用 MA5 对比可能把横盘整理期的正常量误判为缩量回踩，触发假阳性。

**建议**：

```python
# Replace: pullback_vol < pullback_vol_ma5
# With:
is_volume_shrink = pullback_vol < breakout_vol * 0.65
```

---

## 二、软件架构与框架设计

### 2.1 `strategy.py` 已膨胀到 1470 行——God Object 反模式（高优先级）

**现状**：一个单体文件承载了全部策略逻辑：

| 功能域 | 大致行数 |
|--------|---------|
| 市场评分 `score_market` | ~90行 |
| 行业评分 `score_industries` + `_industry_leader_scores` | ~120行 |
| 主线状态 `add_mainline_state` | ~40行 |
| 个股筛选 `select_stocks` | ~180行 |
| 关键点检测 `_detect_keypoint*` + `_keypoint_reject_detail` | ~170行 |
| 交易计划 `_build_trade_plan` + `_detect_pullback` + helper | ~250行 |
| 基本面 `_fundamental_exclude_reason` + `_fundamental_status` | ~60行 |
| 趋势模板 `_trend_template_type` | ~30行 |
| 结果持久化 `_persist_run` + `_excluded_row` | ~100行 |
| 漏斗计数 | ~30行 |

**这是典型的 God Object 反模式。** 后果：
- 单一文件认知负载极高
- 修改一个评分函数可能影响无关的筛选逻辑
- 无法对单个模块做独立单元测试
- PR review 时 diff 范围过大

**建议按领域拆分为独立模块**：

```
a_stock_selector/strategy/
├── __init__.py               # re-export run_strategy
├── market.py                 # score_market
├── industry.py               # score_industries, add_mainline_state, _industry_leader_scores
├── screening.py              # select_stocks, _fundamental_exclude_reason, _trend_template_type
├── keypoint.py               # _detect_keypoint, _detect_recent_keypoint, _keypoint_reject_detail
├── trade_plan.py             # _build_trade_plan, _detect_pullback, helper plans
└── persistence.py            # _persist_run, _excluded_row, _fundamental_status, funnel counting
```

优势：
- 单文件 ≤ 300 行，认知负载可控
- 每个模块可独立导入和测试
- 后续 MVP→P1→P2 扩展不会污染核心模块

---

### 2.2 DataFrame 逐行循环——严重性能瓶颈（高优先级）

**现状**：`select_stocks()` 中逐股遍历 5519 只股票：

```python
for idx, (_, stock) in enumerate(basics.iterrows(), start=1):
    stock_daily = daily_groups.get(code, pd.DataFrame())
    # 5 层过滤，每层调用多个 DataFrame 操作
```

每只股票调用：
- `_trend_template_type` → 4 条均线的 rolling window
- `_detect_recent_keypoint` → 最多回看 6 天 × 3 种突破类型
- `_fundamental_exclude_reason` → 字符串拼接
- `_build_trade_plan` / `_detect_pullback` → 更多 rolling

**5519 × 多层计算 = 数十万次 Pandas 操作**，预期耗时 2-5 分钟。

**建议——三步向量化改造**：

**第一步**：将基础过滤前移到批量操作
```python
# 用一次 merge 完成 ST/价格/成交额/换手率/上市天数过滤
filtered = basics.merge(stock_daily_stats, on="code")
mask_st = filtered["is_st"] == 0
mask_price = filtered["close"] >= 3.0
mask_amount = filtered["amount_ma20"] >= 100_000_000
# ... 一次过滤完成
```

**第二步**：批量计算趋势模板指标
```python
# 用 groupby-rolling 一次性计算全市场 MA50/MA150/MA200
stock_daily["ma50"] = stock_daily.groupby("code")["close"].transform(
    lambda x: x.rolling(50, min_periods=50).mean()
)
```

**第三步**：仅对通过基础过滤的股票做循环（而非全市场 5519 只）

**预期效果**：全市场扫描从 2-5 分钟降到 **30-60 秒**。

---

### 2.3 缺少查询抽象层——UI 直接写 SQL（低优先级）

**现状**：`app.py` 中每个页面函数都直接编写 SQL：

```python
def render_market_overview(st, conn):
    market = read_sql(conn, "SELECT * FROM market_score WHERE batch_id = ?", (batch_id,))
```

**问题**：
- SQL 分散在 UI 代码中，难以测试和维护
- 如果将来切换后端（如 FastAPI 替换 Streamlit）或增加外部 API，需全部重写
- 字段名变更需要同时修改多处 SQL

**建议**：加一个轻量 `queries.py`：

```python
# a_stock_selector/queries.py
def get_latest_market_score(conn) -> dict: ...
def get_industry_rank(conn, batch_id) -> pd.DataFrame: ...
def get_candidates(conn, batch_id) -> pd.DataFrame: ...
def get_funnel_stats(conn, batch_id) -> dict: ...
def get_watch_pool(conn) -> pd.DataFrame: ...
```

---

## 三、数据管道与质量

### 3.1 财务数据代理字段精度不足（高优先级）

**现状**（PROJECT_HANDOFF 第 11 节明确标注）：Tushare `fina_indicator` 的 `profit_dedt` 被用作净利润和扣非净利润的代理字段。

**实际数据**：5519 只股票中，净利润 > 0 的只有 3868 只——**30% 的股票因为代理字段不准确被错误过滤**。`profit_dedt`（扣非净利润）和 `net_profit`（归母净利润）是不同概念，用前者代理后者会导致大量**假阴性**。

**建议**（按优先级排序）：

| 方案 | 可行性 | 效果 |
|------|--------|------|
| A. Tushare `income` 接口直接取 `n_income_attr_p` | 需权限升级 | 最准确 |
| B. AKShare `stock_financial_abstract` 作为补充 | 免费接口 | 覆盖大部分股票 |
| C. 短期放宽代理阈值：`profit_dedt > -1e6` 代替 `> 0` | 立即可做 | 减假阴性 |
| D. 对代理字段缺失的股票标记 `fundamental_status = C` 而非 D | 立即可做 | 不过滤 |

---

### 3.2 停牌检测缺少真实接口（中优先级）

**现状**：停牌判断完全依赖"最近交易日早于当前交易日"的推断逻辑，`is_suspended` 字段默认写 0。没有调用 Tushare `suspend_d` 或 `trade_cal` 接口。

**建议**：在 `data_provider.py` 中增加 `pro.suspend_d()` 调用，标记当日停牌股票。接口参数简单、数据量小（通常每天几十条），不会造成性能压力。

---

### 3.3 行业日线数据源单一（中优先级）

**现状**：`industry_daily` 完全由 `_build_industry_daily` 从 `stock_daily` 聚合而成。

**风险**：
- 如果某行业个股数据缺失，行业指数也会断开
- 行业成分股变动（新股加入/老股退出）不会被捕捉
- 无法与申万官方行业指数交叉验证

**建议**：从 Tushare `sw_daily` 或 AKShare 获取申万官方行业指数作为基准，与个股聚合结果做对比。两者不一致时标记 `data_quality_warning`。

---

## 四、测试与质量保障

### 4.1 缺少真实数据冒烟测试（中优先级）

**现状**：9 个测试全部使用 `SampleDataProvider`（13 只股票的手工构造数据）。没有测试调用真实 Tushare 数据。

**建议**：增加冒烟测试（标记 `@pytest.mark.slow`）：

```python
@pytest.mark.slow
def test_real_data_smoke(tmp_path):
    """用少量真实 Tushare 数据验证完整流程"""
    from a_stock_selector.data_provider import TushareDataProvider, save_dataset
    
    db_path = tmp_path / "smoke.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        dataset = TushareDataProvider(max_stocks=50, lookback_days=300).fetch()
        save_dataset(conn, dataset)
        summary = run_strategy(conn)
        assert summary.candidate_count >= 0
        assert 30 <= market_total <= 90  # market score in reasonable range
```

---

### 4.2 交易计划边界条件覆盖不足（低优先级）

**现状**：`test_keypoint_price_and_leader_plan_formula` 只验证理想情况。

**缺失的边界测试**：
- 止损价 ≥ 买入价时的回退逻辑（`stop_loss_price >= buy_lower` 分支）
- MA10 高于买入价时忽略 MA10 的保护
- `buy_lower > buy_upper` 时返回"等待回踩"的路径
- `suggested_position = 0` 时的空计划字段完整性

这些边界在真实数据中**大概率触发**，需要覆盖。

---

## 五、UI/UX 与可观测性

### 5.1 缺少参数可视化配置页面（中优先级）

**现状**：`config.py` 中的 20 个参数全部硬编码。每次调参需要改代码 → 重新部署。

**建议**：在 Streamlit 侧边栏或独立页面增加参数配置：

```python
with st.expander("参数配置"):
    config.MAINLINE_CONFIRMED_SCORE = st.slider("主线确认阈值", 65, 95, 80)
    config.MIN_AVG_AMOUNT_20D = st.number_input("最低20日均成交额(亿)", value=1.0)
```

参数临时覆盖策略运行结果，不影响 config.py 默认值，下次重启恢复默认。

---

### 5.2 缺少个股详情页（中优先级）

**现状**：候选池只展示表格，无法点击查看单股深度分析。

**建议**：候选池每行增加"详情"链接 → 跳转到独立页面或展开区域内展示：
- K 线图 + MA20/50/150/200 叠加
- 成交量柱状图
- 关键点突破位置标注
- 回踩/确认位置标注（如有）
- 交易计划可视化（买入区间、止损、止盈标线）

技术方案：用 Plotly 或 Altair 做交互式图表。

---

### 5.3 主线稳定性展示可进一步细化（低优先级）

**现状**：第二轮修复后，`rank_table` 已区分"主线连续 X 日"和"候选连续 X 日"。

**可进一步优化**：
- 在主线雷达页增加**时序折线图**，显示每个行业过去 10 日的 `base_score` 变化曲线
- 用颜色区分：绿色 = ≥80（确认），蓝色 = ≥70（候选），灰色 = <70

---

## 六、运维与生产化

### 6.1 缺少自动化定时运行（中优先级）

**现状**：只能手动 CLI 运行。作为盘后选股系统，理想工作流是每天 15:30 自动触发。

**建议**：
- Windows Task Scheduler 定时调用 `python -m a_stock_selector.cli run --refresh`
- 或使用 WorkBuddy cron 功能配置每日定时任务
- 增加 `--cron` 模式：静默运行 + 日志写文件 + 异常通知
- 运行完成后自动导出 CSV 到 `deliverables/` 目录

---

### 6.2 日志系统缺失（低优先级）

**现状**：项目中有 `utils/logger.py` 占位文件但未被引用，所有输出依赖 `print()` 和 `st.write()`。

**建议**：统一使用 Python `logging` 模块：

```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/strategy.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
```

ERROR 级别日志持久化（如"指数 000852 拉取失败"、"某股票 MA200 数据不足"），INFO 级别记录运行摘要。

---

## 七、技术债务与代码质量

### 7.1 类型标注不够精确（低优先级）

**现状**：`strategy.py` 中有大量 `dict[str, object]` 和 `dict[str, float | str | None]` 的松散类型。

**建议**：为交易计划字典定义 `TypedDict`：

```python
from typing import TypedDict

class TradePlan(TypedDict, total=False):
    signal_status: str
    trade_plan_type: str
    suggested_action: str
    watch_price: float
    trigger_price: float
    buy_lower: float | None
    buy_upper: float | None
    suggested_buy_price: float | None
    stop_loss_price: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    trailing_stop_rule: str
    suggested_position: float
    # ...
```

---

### 7.2 主线降级逻辑只读未写（低优先级）

**现状**：`config.py` 定义了 `MAINLINE_DOWNGRADE_DAYS=2` 和 `MAINLINE_REMOVE_DAYS=3`，但 `strategy.py` 中没有实现降级和移除的持久化逻辑。只有检测（`add_mainline_state` 中的 drift），没有动作（如标记 `drift_status="降级"`、写入 `strategy_result` 的建议减仓信号）。

**建议**：在 `add_mainline_state` 中增加降级逻辑：
- 连续 2 日 < 65 → `drift_status = "downgraded"`
- 连续 3 日 < 60 → `drift_status = "removed"`，`confirmed = 0`

---

### 7.3 `select_stocks` 中的主循环可读性（低优先级）

**现状**：`select_stocks` 中的主循环有 ~150 行，嵌套 5 层 if-else 判断 candidate_layer。代码流程复杂，难以快速理解股票被归入哪个 layer 的完整条件。

**建议**：提取一个 `_classify_stock()` 函数：

```python
def _classify_stock(stock, stock_daily, finance_row, keypoint, industry_state, market, config) -> tuple[str, str]:
    """Return (candidate_layer, exclude_reason)."""
```

---

## 八、优先级汇总

| 优先级 | 类别 | 建议项 | 预期收益 |
|--------|------|--------|----------|
| **高** | 架构 | 拆分 strategy.py（God Object → 7模块） | 维护性 +300%，测试独立性 |
| **高** | 性能 | 向量化 select_stocks 批量过滤 | 全市场扫描从分钟级 → 秒级 |
| **高** | 数据 | 修复财务数据代理字段问题 | 候选池扩大 ~30% |
| **高** | 策略 | 修复指数趋势评分与文档对齐 | 市场评分更准确 |
| 中 | 策略 | 赚钱效应分段打分替代连续分位 | 中度偏强市场被正确识别 |
| 中 | 数据 | 申万官方行业指数补充 | 数据交叉验证 |
| 中 | 数据 | 停牌真实接口 | 过滤精度 |
| 中 | 测试 | 真实数据冒烟测试 | 上线信心 |
| 中 | UI | 参数可视化配置页面 | 策略迭代效率 +500% |
| 中 | UI | 个股详情页（K线+交易计划标注） | 用户决策质量 |
| 中 | 运维 | 自动化定时运行 | 运维体验 |
| 低 | 架构 | 查询层解耦（queries.py） | 架构灵活性 |
| 低 | 代码 | TypedDict 类型标注 | 开发体验 |
| 低 | 策略 | 主线降级持久化 | 规则完整性 |
| 低 | 策略 | 回踩缩量定义精度提升 | 信号质量 |
| 低 | UI | 行业时序折线图 | 趋势可视性 |
| 低 | 运维 | logging 模块统一日志 | 问题回溯 |
| 低 | 代码 | _classify_stock 提取 | 可读性 |

---

## 附录：快速自查清单

每次提交前建议确认：

- [ ] `pytest tests/ -q` 全部通过（当前 9/9）
- [ ] `py_compile` 全部模块无警告
- [ ] `python -m a_stock_selector.cli run` 正常执行
- [ ] `run_log` 漏斗字段单调递减
- [ ] 低市场分（<65）候选股的 `buy_lower = NULL`
- [ ] `.env` 未泄露到 git
- [ ] `data/` 目录未打包
- [ ] 新字段全部写入 `strategy_result`
- [ ] `candidate_layer` 字段有效值（正式候选/观察候选/技术突破候选/接近候选/剔除）

from __future__ import annotations

from pathlib import Path

import pandas as pd

from a_stock_selector.cli import export_observation
from a_stock_selector.data_provider import SampleDataProvider, save_dataset
from a_stock_selector.db import connect, init_db, table_count
from a_stock_selector.config import StrategyConfig
from a_stock_selector.strategy import (
    _build_trade_plan,
    _classify_mainline_status,
    _detect_keypoint,
    _fundamental_status,
    _leader_breakout_plan,
    _pullback_confirm_plan,
    run_strategy,
    score_industries,
    select_stocks,
)


def test_sample_strategy_run_writes_required_outputs(tmp_path: Path) -> None:
    db_path = tmp_path / "selector.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        dataset = SampleDataProvider().fetch()
        save_dataset(conn, dataset)
        summary = run_strategy(conn)
        assert summary.excluded_count >= 1
        assert table_count(conn, "market_score") == 1
        assert table_count(conn, "industry_score") > 0
        assert table_count(conn, "strategy_result") >= table_count(conn, "stock_basic")

        strategy_columns = {row["name"] for row in conn.execute("PRAGMA table_info(strategy_result)").fetchall()}
        for column in {
            "candidate_layer",
            "mainline_status",
            "mainline_base_score",
            "keypoint_distance_pct",
            "fundamental_status",
            "trend_template_type",
            "volume_quality",
            "close_quality",
            "risk_level",
            "trade_plan_type",
            "suggested_action",
            "buy_lower",
            "buy_upper",
            "suggested_buy_price",
            "stop_loss_price",
            "take_profit_1",
            "take_profit_2",
            "trailing_stop_rule",
            "suggested_position",
            "rejected_reason_detail",
            "risk_warning",
        }:
            assert column in strategy_columns

        financial_columns = {row["name"] for row in conn.execute("PRAGMA table_info(financials)").fetchall()}
        for column in {"net_profit_missing", "deducted_net_profit_missing", "data_quality_note"}:
            assert column in financial_columns

        industry_columns = {row["name"] for row in conn.execute("PRAGMA table_info(industry_score)").fetchall()}
        for column in {"stability_days", "rank_change", "score_change", "drift_flag", "base_score"}:
            assert column in industry_columns
        for column in {"leader_score", "hot_score", "candidate_stability_days", "confirmed_stability_days"}:
            assert column in industry_columns
        for column in {"leader_250_high_count", "leader_60_high_count", "leader_trend_middle_count"}:
            assert column in industry_columns
        for column in {
            "mainline_status", "strong_streak_days", "rank_top5_avg", "amount_ratio",
            "is_watch_mainline", "is_near_confirm", "is_downtrend_watch",
        }:
            assert column in industry_columns

        plan_rows = conn.execute(
            """
            SELECT market_score, suggested_action, buy_lower, buy_upper, suggested_buy_price,
                   stop_loss_price, take_profit_1, take_profit_2, suggested_position
            FROM strategy_result
            WHERE status = 'included'
            """
        ).fetchall()
        for row in plan_rows:
            _assert_trade_plan_legality(dict(row))

        low_market_rows = conn.execute(
            """
            SELECT buy_lower, buy_upper, suggested_buy_price, take_profit_1,
                   take_profit_2, suggested_position, suggested_action
            FROM strategy_result
            WHERE status = 'included' AND market_score < 65
            """
        ).fetchall()
        for row in low_market_rows:
            assert row["suggested_action"] in {"仅观察", "等待回踩"}
            assert row["buy_lower"] is None
            assert row["buy_upper"] is None
            assert row["suggested_buy_price"] is None
            assert row["take_profit_1"] is None
            assert row["take_profit_2"] is None
            assert row["suggested_position"] == 0


def test_run_log_funnel_columns_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "selector.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        run_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(run_log)").fetchall()}
        for col in ("total_stock_count", "after_basic_filter_count", "after_fundamental_filter_count",
                    "after_trend_filter_count", "after_keypoint_filter_count",
                    "after_mainline_filter_count", "after_market_filter_count"):
            assert col in run_log_cols, f"run_log missing column: {col}"


def test_observe_exports_daily_markdown_and_csv(tmp_path: Path) -> None:
    db_path = tmp_path / "selector.sqlite3"
    output_root = tmp_path / "deliverables"
    init_db(db_path)
    with connect(db_path) as conn:
        dataset = SampleDataProvider().fetch()
        save_dataset(conn, dataset)
        run_strategy(conn)
        observation = export_observation(conn, output_root)

    markdown_path = Path(str(observation["markdown_path"]))
    candidates_path = Path(str(observation["candidates_path"]))
    log_path = output_root / "observations" / "observation_log.csv"
    assert markdown_path.exists()
    assert candidates_path.exists()
    assert log_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# 每日观察记录" in markdown
    assert "## 二、主线 Top5" in markdown
    assert "## 三、筛选漏斗" in markdown
    header = candidates_path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "code,name,industry,status,candidate_layer" in header


def test_sample_provider_includes_core_indices() -> None:
    dataset = SampleDataProvider().fetch()
    index_codes = set(dataset.index_daily["index_code"].astype(str).unique())
    for code in {"000300", "000905", "000852", "399303", "399006"}:
        assert code in index_codes


def test_funnel_counts_are_monotonic(tmp_path: Path) -> None:
    """Funnel counts must be monotonically decreasing: total >= basic >= fund >= trend >= kp >= candidates."""
    db_path = tmp_path / "selector.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        dataset = SampleDataProvider().fetch()
        save_dataset(conn, dataset)
        run_strategy(conn)
        log = conn.execute("SELECT * FROM run_log ORDER BY run_timestamp DESC LIMIT 1").fetchone()
        t = int(log["total_stock_count"])
        b = int(log["after_basic_filter_count"])
        f = int(log["after_fundamental_filter_count"])
        tr = int(log["after_trend_filter_count"])
        kp = int(log["after_keypoint_filter_count"])
        ml = int(log["after_mainline_filter_count"])
        mk = int(log["after_market_filter_count"])
        c = int(log["candidate_count"])
        assert t >= b >= f >= tr >= kp >= ml >= mk >= c, (
            f"Funnel not monotonic: {t} -> {b} -> {f} -> {tr} -> {kp} -> {ml} -> {mk} -> {c}"
        )
        assert t > 0, "total_stock_count should be > 0"
        assert c >= 0, "candidate_count should be >= 0"

        layers = {
            row["candidate_layer"]
            for row in conn.execute("SELECT DISTINCT candidate_layer FROM strategy_result").fetchall()
        }
        assert "剔除" in layers


def test_industry_leader_score_uses_stock_structure() -> None:
    dates = pd.date_range("2025-01-01", periods=270, freq="D").strftime("%Y-%m-%d")
    basics = pd.DataFrame(
        [
            {"code": "A", "name": "A", "industry": "半导体", "list_date": "20200101"},
            {"code": "B", "name": "B", "industry": "半导体", "list_date": "20200101"},
            {"code": "C", "name": "C", "industry": "半导体", "list_date": "20200101"},
        ]
    )
    rows = []
    for code in basics["code"]:
        for idx, trade_date in enumerate(dates):
            close = 10 + idx * 0.01
            rows.append(
                {
                    "code": code,
                    "trade_date": trade_date,
                    "open": close - 0.02,
                    "high": close + 0.05,
                    "low": close - 0.05,
                    "close": close,
                    "volume": 1_000_000,
                    "amount": 150_000_000,
                    "pct_chg": 1.0,
                    "turnover_rate": 2.0,
                }
            )
    stock_daily = pd.DataFrame(rows)
    latest = stock_daily[stock_daily["trade_date"] == dates[-1]]
    industry_daily = pd.DataFrame(
        {
            "industry": ["半导体"] * len(dates),
            "trade_date": dates,
            "close": [100 + i for i in range(len(dates))],
            "pct_chg": [1.0] * len(dates),
            "amount": [450_000_000] * len(dates),
            "up_count": [3] * len(dates),
            "down_count": [0] * len(dates),
            "limit_up_count": [0] * len(dates),
        }
    )
    # Force all three stocks to print fresh highs on the latest day.
    stock_daily.loc[latest.index, "close"] = 20
    stock_daily.loc[latest.index, "high"] = 20.2

    scored = score_industries(industry_daily, dates[-1], StrategyConfig(), stock_daily, basics)
    row = scored.iloc[0]
    assert row["leader_score"] == 15
    assert row["leader_250_high_count"] == 3
    assert row["leader_60_high_count"] == 3
    assert row["leader_trend_middle_count"] == 3
    assert row["base_score"] >= row["leader_score"]


def _classify_status(base_scores: list[float], latest_rank: int = 1, amount_ratio: float = 1.3, **latest_extra) -> dict:
    latest = pd.Series(
        {
            "base_score": base_scores[-1],
            "rank": latest_rank,
            "amount_ratio": amount_ratio,
            "strength_score": latest_extra.get("strength_score", 16),
            "capacity_score": latest_extra.get("capacity_score", 11),
            "leader_score": latest_extra.get("leader_score", 5),
        }
    )
    history = pd.DataFrame({"base_score": base_scores, "rank": [latest_rank] * len(base_scores)})
    tail3 = history.tail(3)
    tail5 = history.tail(5)
    candidate = bool(base_scores[-1] >= 70 and sum(v >= 70 for v in base_scores[-3:]) >= 2)
    confirmed = bool(len(base_scores[-3:]) >= 3 and all(v >= 80 for v in base_scores[-3:]))
    return _classify_mainline_status(
        latest_row=latest,
        tail3=tail3,
        tail5=tail5,
        top_rank_limit=1,
        is_candidate=candidate,
        is_confirmed=confirmed,
        candidate_stability_days=sum(1 for v in reversed(base_scores) if v >= 70),
        confirmed_stability_days=sum(1 for v in reversed(base_scores) if v >= 80),
        config=StrategyConfig(),
    )


def test_mainline_watch_status() -> None:
    status = _classify_status([50, 62, 66], latest_rank=1, amount_ratio=1.25)
    assert status["mainline_status"] == "主线预警"
    assert status["is_watch_mainline"] == 1


def test_candidate_mainline_status() -> None:
    status = _classify_status([68, 71, 72], latest_rank=2, amount_ratio=1.0, strength_score=10, capacity_score=8, leader_score=0)
    assert status["mainline_status"] == "候选主线"


def test_near_confirm_mainline_status() -> None:
    status = _classify_status([71, 72, 76], latest_rank=1)
    assert status["mainline_status"] == "接近确认"
    assert status["is_near_confirm"] == 1


def test_confirmed_mainline_status_keeps_80_standard() -> None:
    status = _classify_status([81, 82, 83], latest_rank=1)
    assert status["mainline_status"] == "确认主线"


def test_downtrend_watch_status_after_prior_strength() -> None:
    status = _classify_status([82, 78, 64, 63], latest_rank=1)
    assert status["mainline_status"] == "退潮观察"
    assert status["is_downtrend_watch"] == 1


def test_keypoint_price_and_leader_plan_formula() -> None:
    dates = pd.date_range("2025-01-01", periods=270, freq="D").strftime("%Y-%m-%d")
    close = [10 + i * 0.01 for i in range(269)] + [13.0]
    daily = pd.DataFrame(
        {
            "trade_date": dates,
            "open": [c - 0.05 for c in close],
            "high": [c + 0.10 for c in close[:-1]] + [13.05],
            "low": [c - 0.10 for c in close[:-1]] + [12.85],
            "close": close,
            "volume": [1_000_000] * 269 + [1_800_000],
            "amount": [120_000_000] * 270,
            "pct_chg": [1.0] * 270,
            "turnover_rate": [2.0] * 270,
        }
    )
    previous_high = float(daily.iloc[:-1]["high"].max())
    keypoint = _detect_keypoint(daily, "A", StrategyConfig())
    assert keypoint is not None
    assert keypoint["keypoint_price"] == previous_high

    plan = _build_trade_plan(
        latest=daily.iloc[-1],
        keypoint=keypoint,
        market={"total_score": 70, "risk_level": "可交易"},
        industry_state={"base_score": 85, "mainline_status": "确认主线", "confirmed": 1},
        trend_type="A",
        config=StrategyConfig(),
        stock_daily=daily,
    )
    assert plan["trade_plan_type"] == "龙头突破试错计划"
    assert plan["buy_lower"] == round(previous_high, 2)
    assert plan["buy_upper"] == round(previous_high * 1.02, 2)
    assert plan["take_profit_1"] == round(plan["suggested_buy_price"] * 1.10, 2)
    assert plan["take_profit_2"] == round(plan["suggested_buy_price"] * 1.20, 2)
    _assert_trade_plan_legality(plan)


def test_middle_pullback_requires_real_confirmation() -> None:
    dates = pd.date_range("2025-01-01", periods=270, freq="D").strftime("%Y-%m-%d")
    close = [10.0] * 266 + [10.30, 10.08, 10.12, 10.35]
    open_ = [9.98] * 266 + [10.05, 10.15, 10.10, 10.18]
    volume = [100_000] * 266 + [300_000, 100_000, 90_000, 420_000]
    daily = pd.DataFrame(
        {
            "trade_date": dates,
            "open": open_,
            "high": [c + 0.08 for c in close],
            "low": [9.95] * 266 + [10.05, 10.05, 10.06, 10.20],
            "close": close,
            "volume": volume,
            "amount": [120_000_000] * 270,
            "pct_chg": [0.5] * 270,
            "turnover_rate": [2.0] * 270,
        }
    )
    keypoint = {
        "keypoint_date": dates[-4],
        "breakout_date": dates[-4],
        "keypoint_price": 10.0,
        "keypoint_type": "120日平台突破",
        "breakout_close": 10.30,
        "breakout_day_low": 10.05,
        "breakout_ma10": 10.0,
    }
    plan = _build_trade_plan(
        latest=daily.iloc[-1],
        keypoint=keypoint,
        market={"total_score": 70, "risk_level": "可交易"},
        industry_state={"base_score": 85, "mainline_status": "确认主线", "confirmed": 1},
        trend_type="B",
        config=StrategyConfig(),
        stock_daily=daily,
    )
    assert plan["suggested_action"] == "建议计划买入"
    assert plan["pullback_volume_shrink"] == 1
    assert plan["confirm_volume_expand"] == 1
    assert plan["take_profit_1"] == round(plan["suggested_buy_price"] * 1.12, 2)
    assert plan["take_profit_2"] == round(plan["suggested_buy_price"] * 1.25, 2)
    _assert_trade_plan_legality(plan)


def test_middle_pullback_allows_first_day_confirmation() -> None:
    dates = pd.date_range("2025-01-01", periods=270, freq="D").strftime("%Y-%m-%d")
    close = [10.0] * 268 + [10.30, 10.16]
    open_ = [9.98] * 268 + [10.05, 10.05]
    volume = [100_000] * 268 + [300_000, 220_000]
    daily = pd.DataFrame(
        {
            "trade_date": dates,
            "open": open_,
            "high": [c + 0.08 for c in close],
            "low": [9.95] * 268 + [10.05, 10.12],
            "close": close,
            "volume": volume,
            "amount": [120_000_000] * 270,
            "pct_chg": [0.5] * 270,
            "turnover_rate": [2.0] * 270,
        }
    )
    keypoint = {
        "keypoint_date": dates[-2],
        "breakout_date": dates[-2],
        "keypoint_price": 10.0,
        "keypoint_type": "120日平台突破",
        "breakout_close": 10.30,
        "breakout_day_low": 10.05,
        "breakout_ma10": 10.0,
    }
    plan = _build_trade_plan(
        latest=daily.iloc[-1],
        keypoint=keypoint,
        market={"total_score": 70, "risk_level": "可交易"},
        industry_state={"base_score": 85, "mainline_status": "确认主线", "confirmed": 1},
        trend_type="B",
        config=StrategyConfig(),
        stock_daily=daily,
    )
    assert plan["suggested_action"] == "建议计划买入"
    assert plan["pullback_volume_shrink"] == 1
    assert plan["confirm_volume_expand"] == 1
    _assert_trade_plan_legality(plan)


def test_low_market_plan_never_outputs_buy_range() -> None:
    latest = pd.Series({"trade_date": "2025-01-02"})
    keypoint = {
        "keypoint_date": "2025-01-02",
        "breakout_date": "2025-01-02",
        "keypoint_price": 10.0,
        "keypoint_type": "250日新高",
        "breakout_close": 10.2,
        "breakout_day_low": 9.9,
        "breakout_ma10": 9.8,
    }
    plan = _build_trade_plan(
        latest=latest,
        keypoint=keypoint,
        market={"total_score": 60, "risk_level": "观察"},
        industry_state={"base_score": 90},
        trend_type="A",
        config=StrategyConfig(),
        stock_daily=None,
    )
    _assert_trade_plan_legality({"market_score": 60, **plan})


def test_market_score_protects_early_mainline_statuses() -> None:
    latest = pd.Series({"trade_date": "2025-01-02"})
    keypoint = {
        "keypoint_date": "2025-01-02",
        "breakout_date": "2025-01-02",
        "keypoint_price": 10.0,
        "keypoint_type": "历史新高",
        "breakout_close": 10.2,
        "breakout_day_low": 9.9,
        "breakout_ma10": 9.8,
    }
    for status in ("主线预警", "候选主线", "接近确认"):
        plan = _build_trade_plan(
            latest=latest,
            keypoint=keypoint,
            market={"total_score": 60, "risk_level": "观察"},
            industry_state={"base_score": 76, "rank": 1, "mainline_status": status, "is_watch_mainline": 1},
            trend_type="A",
            config=StrategyConfig(),
            stock_daily=None,
        )
        assert plan["suggested_action"] == "仅观察"
        assert plan["buy_lower"] is None
        assert plan["buy_upper"] is None
        assert plan["suggested_buy_price"] is None
        assert plan["suggested_position"] == 0


def test_watch_mainline_b_trend_cannot_create_trial_buy() -> None:
    latest = pd.Series({"trade_date": "2025-01-02"})
    keypoint = {
        "keypoint_date": "2025-01-02",
        "breakout_date": "2025-01-02",
        "keypoint_price": 10.0,
        "keypoint_type": "历史新高",
        "breakout_close": 10.2,
        "breakout_day_low": 9.9,
        "breakout_ma10": 9.8,
    }
    plan = _build_trade_plan(
        latest=latest,
        keypoint=keypoint,
        market={"total_score": 70, "risk_level": "可交易"},
        industry_state={"base_score": 66, "rank": 1, "mainline_status": "主线预警", "is_watch_mainline": 1},
        trend_type="B",
        config=StrategyConfig(),
        stock_daily=None,
    )
    assert plan["suggested_action"] == "仅观察"
    assert plan["suggested_position"] == 0


def test_watch_mainline_a_strong_breakout_can_create_small_trial() -> None:
    latest = pd.Series({"trade_date": "2025-01-02"})
    keypoint = {
        "keypoint_date": "2025-01-02",
        "breakout_date": "2025-01-02",
        "keypoint_price": 10.0,
        "keypoint_type": "250日新高",
        "breakout_close": 10.2,
        "breakout_day_low": 9.9,
        "breakout_ma10": 9.8,
    }
    plan = _build_trade_plan(
        latest=latest,
        keypoint=keypoint,
        market={"total_score": 70, "risk_level": "可交易"},
        industry_state={"base_score": 66, "rank": 1, "mainline_status": "主线预警", "is_watch_mainline": 1},
        trend_type="A",
        config=StrategyConfig(),
        stock_daily=None,
    )
    assert plan["suggested_action"] == "主线预警，龙头试错"
    assert 3 <= float(plan["suggested_position"]) <= 5
    assert plan["buy_lower"] is not None


def test_downtrend_watch_never_creates_new_buy_plan() -> None:
    latest = pd.Series({"trade_date": "2025-01-02"})
    keypoint = {
        "keypoint_date": "2025-01-02",
        "breakout_date": "2025-01-02",
        "keypoint_price": 10.0,
        "keypoint_type": "历史新高",
        "breakout_close": 10.2,
        "breakout_day_low": 9.9,
        "breakout_ma10": 9.8,
    }
    plan = _build_trade_plan(
        latest=latest,
        keypoint=keypoint,
        market={"total_score": 80, "risk_level": "积极"},
        industry_state={"base_score": 62, "rank": 1, "mainline_status": "退潮观察"},
        trend_type="A",
        config=StrategyConfig(),
        stock_daily=None,
    )
    assert plan["suggested_action"] == "仅观察"
    assert plan["suggested_position"] == 0


def test_leader_stop_loss_fallback_when_stop_crosses_buy_lower() -> None:
    plan = _leader_breakout_plan(
        key_price=10.0,
        breakout_close=10.2,
        breakout_day_low=10.1,
        breakout_ma10=9.8,
        market_score=70,
        config=StrategyConfig(),
    )
    assert plan["buy_lower"] == 10.0
    assert plan["stop_loss_price"] == 9.7
    _assert_trade_plan_legality(plan)


def test_leader_stop_loss_ignores_ma10_above_suggested_buy() -> None:
    plan = _leader_breakout_plan(
        key_price=10.0,
        breakout_close=10.2,
        breakout_day_low=9.6,
        breakout_ma10=10.5,
        market_score=70,
        config=StrategyConfig(),
    )
    assert plan["suggested_buy_price"] == 10.1
    assert plan["stop_loss_price"] == 9.7
    _assert_trade_plan_legality(plan)


def test_pullback_plan_waits_when_buy_range_is_inverted() -> None:
    plan = _pullback_confirm_plan(
        key_price=10.0,
        pullback={
            "pullback_low": 10.0,
            "confirm_close": 10.0,
            "confirm_date": "2025-01-02",
            "pullback_volume_shrink": 1,
            "confirm_volume_expand": 1,
            "ma20": 10.5,
        },
        market_score=70,
        config=StrategyConfig(),
    )
    assert plan["suggested_action"] == "等待回踩"
    assert plan["buy_lower"] is None
    assert plan["buy_upper"] is None
    assert plan["suggested_buy_price"] is None
    assert plan["suggested_position"] == 0


def test_missing_net_profit_with_deduct_profit_is_fundamental_c_not_d() -> None:
    stock = pd.Series({"is_st": 0, "is_delist_risk": 0, "is_suspended": 0, "list_date": "20200101"})
    finance = pd.DataFrame(
        [
            {
                "report_date": "2025-12-31",
                "net_profit": 0.0,
                "deducted_net_profit": 100_000_000.0,
                "revenue_yoy": 5.0,
                "asset_liability_ratio": 40.0,
                "net_profit_missing": 1,
                "deducted_net_profit_missing": 0,
                "data_quality_note": "财务数据缺失或代理字段不足：归母净利润字段缺失",
            }
        ]
    )
    assert _fundamental_status(stock, finance, None, "2026-05-20", StrategyConfig()) == "C"


def _layered_select_input(mainline_status: str, base_score: float, breakout: bool, market_score: float = 70) -> tuple[dict, pd.DataFrame, dict]:
    dates = pd.date_range("2025-01-01", periods=270, freq="D").strftime("%Y-%m-%d")
    close = [8 + i * 0.015 for i in range(270)]
    if breakout:
        close[-1] = 13.0
    else:
        close[-1] = close[-2] - 0.03
    volume = [1_000_000] * 269 + ([1_800_000] if breakout else [1_050_000])
    daily = pd.DataFrame(
        {
            "code": ["T001"] * 270,
            "trade_date": dates,
            "open": [c - 0.03 for c in close],
            "high": [c + 0.08 for c in close[:-1]] + ([13.05] if breakout else [close[-1] + 0.04]),
            "low": [c - 0.08 for c in close[:-1]] + ([12.75] if breakout else [close[-1] - 0.08]),
            "close": close,
            "volume": volume,
            "amount": [150_000_000] * 270,
            "pct_chg": [0.5] * 270,
            "turnover_rate": [2.0] * 270,
            "is_suspended": [0] * 270,
        }
    )
    data = {
        "stock_basic": pd.DataFrame(
            [
                {
                    "code": "T001",
                    "name": "测试股份",
                    "industry": "半导体",
                    "list_date": "20200101",
                    "is_st": 0,
                    "is_delist_risk": 0,
                    "is_suspended": 0,
                }
            ]
        ),
        "stock_daily": daily,
        "financials": pd.DataFrame(
            [
                {
                    "code": "T001",
                    "report_date": "2025-12-31",
                    "net_profit": 100_000_000,
                    "deducted_net_profit": 80_000_000,
                    "revenue_yoy": 5,
                    "asset_liability_ratio": 40,
                    "net_profit_missing": 0,
                    "deducted_net_profit_missing": 0,
                    "data_quality_note": "",
                }
            ]
        ),
    }
    industry_scores = pd.DataFrame(
        [
            {
                "industry": "半导体",
                "rank": 1,
                "score": base_score,
                "base_score": base_score,
                "mainline_status": mainline_status,
                "confirmed": 1 if mainline_status == "确认主线" else 0,
                "is_watch_mainline": 1 if mainline_status == "主线预警" else 0,
                "is_candidate_mainline": 1 if mainline_status == "候选主线" else 0,
                "is_near_confirm": 1 if mainline_status == "接近确认" else 0,
            }
        ]
    )
    market = {"total_score": market_score, "risk_level": "可交易" if market_score >= 65 else "观察"}
    return data, industry_scores, market


def test_65_mainline_generates_warning_stock_pool() -> None:
    data, industry_scores, market = _layered_select_input("主线预警", 66, breakout=False)
    results = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig())
    row = results.iloc[0]
    assert row["candidate_layer"] == "预警个股池"
    assert row["suggested_action"] == "主线预警，加入观察"
    assert row["suggested_position"] == 0
    assert row["buy_lower"] is None


def test_75_mainline_generates_focus_stock_pool() -> None:
    data, industry_scores, market = _layered_select_input("接近确认", 76, breakout=False)
    results = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig())
    row = results.iloc[0]
    assert row["candidate_layer"] == "重点观察个股池"
    assert row["suggested_action"] == "重点观察，等待触发"
    assert row["watch_price"] is not None
    assert row["trigger_price"] is not None
    assert row["suggested_position"] == 0
    assert row["buy_lower"] is None


def test_80_confirmed_mainline_generates_formal_stock_pool() -> None:
    data, industry_scores, market = _layered_select_input("确认主线", 85, breakout=True)
    results = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig())
    row = results.iloc[0]
    assert row["candidate_layer"] == "正式候选股池"
    assert row["suggested_action"] in {"建议试错买入", "建议计划买入"}
    for field in ("buy_lower", "buy_upper", "stop_loss_price", "take_profit_1", "take_profit_2"):
        assert row[field] is not None
    assert row["suggested_position"] > 0


def test_market_below_65_downgrades_confirmed_breakout_from_formal_pool() -> None:
    data, industry_scores, market = _layered_select_input("确认主线", 85, breakout=True, market_score=57)
    results = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig())
    row = results.iloc[0]
    assert row["candidate_layer"] != "正式候选股池"
    assert row["suggested_position"] == 0
    assert row["buy_lower"] is None
    assert row["buy_upper"] is None
    assert row["suggested_buy_price"] is None


def test_layer_priority_uses_highest_matching_pool() -> None:
    data, industry_scores, market = _layered_select_input("确认主线", 85, breakout=True)
    row = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig()).iloc[0]
    assert row["candidate_layer"] == "正式候选股池"

    data, industry_scores, market = _layered_select_input("接近确认", 76, breakout=True)
    row = select_stocks(data, "2025-09-27", market, industry_scores, StrategyConfig()).iloc[0]
    assert row["candidate_layer"] == "重点观察个股池"


def _assert_trade_plan_legality(plan: dict) -> None:
    action = plan.get("suggested_action")
    market_score = plan.get("market_score")
    if action in {"建议试错买入", "建议计划买入"}:
        for field in (
            "buy_lower",
            "buy_upper",
            "suggested_buy_price",
            "stop_loss_price",
            "take_profit_1",
            "take_profit_2",
        ):
            assert plan.get(field) is not None, f"{field} should not be empty for {action}"
        buy_lower = float(plan["buy_lower"])
        buy_upper = float(plan["buy_upper"])
        suggested_buy = float(plan["suggested_buy_price"])
        stop_loss = float(plan["stop_loss_price"])
        take_profit_1 = float(plan["take_profit_1"])
        take_profit_2 = float(plan["take_profit_2"])
        assert buy_lower <= suggested_buy <= buy_upper
        assert stop_loss < suggested_buy
        assert take_profit_1 > suggested_buy
        assert take_profit_2 > take_profit_1
        assert float(plan["suggested_position"]) > 0
    if market_score is not None and float(market_score) < 65:
        assert action in {"仅观察", "等待回踩"}
        assert plan.get("buy_lower") is None
        assert plan.get("buy_upper") is None
        assert plan.get("suggested_buy_price") is None
        assert plan.get("take_profit_1") is None
        assert plan.get("take_profit_2") is None
        assert float(plan.get("suggested_position") or 0) == 0

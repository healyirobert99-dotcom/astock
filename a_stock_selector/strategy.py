from __future__ import annotations

import sqlite3
import uuid
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import pandas as pd

from .config import RISK_WARNING, StrategyConfig
from .db import read_sql
from .indicators import latest_trade_date, moving_average, pct_rank

ProgressCallback = Callable[[str, int, int], None]


@dataclass
class StrategyRunSummary:
    batch_id: str
    run_timestamp: str
    trade_date: str
    candidate_count: int
    excluded_count: int


def run_strategy(
    conn: sqlite3.Connection,
    config: StrategyConfig | None = None,
    progress_callback: ProgressCallback | None = None,
) -> StrategyRunSummary:
    config = config or StrategyConfig()
    _emit(progress_callback, "加载本地行情与财务数据", 5, 100)
    data = _load_market_data(conn)
    trade_date = latest_trade_date([data["stock_daily"], data["index_daily"], data["industry_daily"]])
    batch_id = uuid.uuid4().hex[:12]
    run_timestamp = datetime.now().isoformat(timespec="seconds")

    _emit(progress_callback, "计算市场环境评分", 15, 100)
    market = score_market(data, trade_date, config)
    _emit(progress_callback, "计算行业主线评分", 28, 100)
    industry_scores = score_industries(
        data["industry_daily"],
        trade_date,
        config,
        stock_daily=data["stock_daily"],
        stock_basic=data["stock_basic"],
    )
    _emit(progress_callback, "检查主线确认与漂移", 40, 100)
    industry_scores = add_mainline_state(conn, industry_scores)
    results = select_stocks(data, trade_date, market, industry_scores, config, progress_callback)

    _emit(progress_callback, "写入策略结果", 92, 100)
    _persist_run(conn, batch_id, run_timestamp, trade_date, market, industry_scores, results)

    # Calculate per-stage funnel counts (cumulative exclusion)
    total_stocks = len(results)
    all_reasons = results["exclude_reason"].fillna("")
    basic_reasons = all_reasons.str.contains("历史行情不足|ST或|退市风险|停牌|股价低于|上市时间不足|成交额不足|换手率不足", na=False)
    fund_reasons = all_reasons.str.contains("净利润|扣非|营收|资产负债|缺少基础财务|财务数据缺失|代理字段不足", na=False)
    trend_reasons = all_reasons.str.contains("C类趋势", na=False)
    keypoint_reasons = all_reasons.str.contains("未形成有效关键点", na=False)

    after_basic = int((~basic_reasons).sum())  # passed basic filter (or not yet excluded)
    after_fundamental = int((~basic_reasons & ~fund_reasons).sum())  # passed basic + fundamental
    after_trend = int((~basic_reasons & ~fund_reasons & ~trend_reasons).sum())
    after_keypoint = int(results["keypoint_date"].notna().sum()) if "keypoint_date" in results else 0
    after_mainline = int(
        results["candidate_layer"]
        .isin(["预警个股池", "重点观察个股池", "正式候选股池", "技术突破候选"])
        .sum()
    )
    after_market = after_mainline if float(market["total_score"]) >= config.MARKET_TRADE_SCORE else 0
    candidate_count = int((results["candidate_layer"] == "正式候选股池").sum())

    conn.execute(
        """
        INSERT INTO run_log (
            batch_id, run_timestamp, trade_date, data_source, status, message,
            total_stock_count, after_basic_filter_count, after_fundamental_filter_count,
            after_trend_filter_count, after_keypoint_filter_count,
            after_mainline_filter_count, after_market_filter_count,
            candidate_count, excluded_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            run_timestamp,
            trade_date,
            _current_data_source(conn),
            "success",
            "strategy completed",
            total_stocks,
            after_basic,
            after_fundamental,
            after_trend,
            after_keypoint,
            after_mainline,
            after_market,
            candidate_count,
            total_stocks - candidate_count,
        ),
    )
    conn.commit()
    _emit(progress_callback, "策略运行完成", 100, 100)
    return StrategyRunSummary(
        batch_id=batch_id,
        run_timestamp=run_timestamp,
        trade_date=trade_date,
        candidate_count=candidate_count,
        excluded_count=total_stocks - candidate_count,
    )


def score_market(
    data: dict[str, pd.DataFrame],
    trade_date: str,
    config: StrategyConfig | None = None,
) -> dict[str, float | str]:
    index_daily = data["index_daily"].copy()
    stock_daily = data["stock_daily"].copy()
    industry_daily = data["industry_daily"].copy()
    latest_stock = stock_daily[stock_daily["trade_date"] == trade_date].copy()

    preferred = ["000300", "000905", "000852", "399303", "399006"]
    available = set(index_daily["index_code"].astype(str)) if not index_daily.empty else set()
    selected = [code for code in preferred if code in available]
    if not selected:
        selected = sorted(available)

    index_component = []
    amount_ratios = []
    for code, group in index_daily[index_daily["index_code"].isin(selected)].groupby("index_code"):
        group = group.sort_values("trade_date").copy()
        if len(group) < 120:
            continue
        group["ma20"] = moving_average(group["close"], 20)
        group["ma60"] = moving_average(group["close"], 60)
        group["ma120"] = moving_average(group["close"], 120)
        group["amount_ma20"] = moving_average(group["amount"], 20)
        last = group.iloc[-1]
        score = 0.0
        score += 8 if last["close"] > last["ma20"] else 0
        score += 8 if last["ma20"] > last["ma60"] else 0
        score += 7 if last["ma60"] > last["ma120"] else 0
        score += 7 if last["ma20"] > group["ma20"].iloc[-6] else 0
        index_component.append(score)
        if last["amount_ma20"] > 0:
            amount_ratios.append(float(last["amount"] / last["amount_ma20"]))
    index_trend_score = float(pd.Series(index_component).mean()) if index_component else 0.0

    up_count = int(latest_stock["pct_chg"].gt(0).sum())
    down_count = int(latest_stock["pct_chg"].lt(0).sum())
    total_count = max(1, up_count + down_count)
    up_ratio = up_count / total_count
    up_down_ratio = up_count / max(1, down_count)
    strong_up = int(latest_stock["pct_chg"].gt(5).sum())
    strong_down = int(latest_stock["pct_chg"].lt(-5).sum())
    limit_up = int(latest_stock.get("is_limit_up", latest_stock["pct_chg"].ge(9.5)).sum())
    limit_down = int(latest_stock.get("is_limit_down", latest_stock["pct_chg"].le(-9.5)).sum())
    profit_effect_score = min(
        30.0,
        pct_rank(up_ratio, 0.35, 0.70) * 0.10
        + pct_rank(up_down_ratio, 0.6, 2.2) * 0.08
        + pct_rank(strong_up / total_count, 0.02, 0.12) * 0.05
        + pct_rank(limit_up - limit_down, -10, 80) * 0.04
        + pct_rank((strong_up - strong_down) / total_count, -0.08, 0.08) * 0.03,
    )

    if not stock_daily.empty:
        daily_amount = stock_daily.groupby("trade_date")["amount"].sum().sort_index()
        amount_ma20 = moving_average(daily_amount, 20)
        latest_amount_ratio = (
            float(daily_amount.iloc[-1] / amount_ma20.iloc[-1]) if len(amount_ma20) and amount_ma20.iloc[-1] > 0 else 1.0
        )
    else:
        latest_amount_ratio = float(pd.Series(amount_ratios).mean()) if amount_ratios else 1.0
    activity_score = pct_rank(latest_amount_ratio, 0.75, 1.35) * 0.15

    limit_balance = limit_up - limit_down
    sentiment_score = min(
        15.0,
        pct_rank(limit_balance, -10, 80) * 0.06
        + pct_rank(limit_up / max(1, total_count), 0.0, 0.04) * 0.05
        + pct_rank(float(latest_stock["pct_chg"].mean()) if not latest_stock.empty else 0.0, -1.5, 2.5) * 0.04,
    )

    # Style consistency: 10 points
    # (a) ≥2 of {沪深300,中证500,中证1000} above MA20 → 5pt
    style_indices = {"000300", "000905", "000852"}
    above_ma20_count = 0
    for code, group in index_daily[index_daily["index_code"].isin(style_indices)].groupby("index_code"):
        g = group.sort_values("trade_date").copy()
        g["ma20"] = moving_average(g["close"], 20)
        if not g.empty and float(g.iloc[-1]["close"]) > float(g.iloc[-1]["ma20"]):
            above_ma20_count += 1
    style_a = 5.0 if above_ma20_count >= 2 else 0.0

    # (b) up ratio consistent with index state → 5pt
    index_up = above_ma20_count >= 2
    up_consistent = (up_ratio >= 0.50) == index_up
    style_b = 5.0 if up_consistent else 0.0

    style_consistency_score = style_a + style_b

    total_score = index_trend_score + profit_effect_score + activity_score + sentiment_score + style_consistency_score
    if total_score >= 75:
        risk_level = "积极"
    elif total_score >= 65:
        risk_level = "可交易"
    elif total_score >= 50:
        risk_level = "观察"
    else:
        risk_level = "谨慎"
    return {
        "index_trend_score": round(index_trend_score, 2),
        "profit_effect_score": round(profit_effect_score, 2),
        "activity_score": round(activity_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "style_consistency_score": round(style_consistency_score, 2),
        "total_score": round(float(total_score), 2),
        "risk_level": risk_level,
    }


def _industry_leader_scores(
    stock_daily: pd.DataFrame | None,
    stock_basic: pd.DataFrame | None,
    config: StrategyConfig,
) -> pd.DataFrame:
    if stock_daily is None or stock_basic is None or stock_daily.empty or stock_basic.empty:
        return pd.DataFrame(
            columns=[
                "industry",
                "trade_date",
                "leader_score",
                "leader_250_high_count",
                "leader_60_high_count",
                "leader_trend_middle_count",
            ]
        )

    code_industry = stock_basic[["code", "industry"]].dropna().drop_duplicates("code")
    daily = stock_daily.merge(code_industry, on="code", how="left")
    daily = daily[daily["industry"].notna()].sort_values(["code", "trade_date"]).copy()
    if daily.empty:
        return pd.DataFrame(
            columns=[
                "industry",
                "trade_date",
                "leader_score",
                "leader_250_high_count",
                "leader_60_high_count",
                "leader_trend_middle_count",
            ]
        )

    groups = daily.groupby("code", group_keys=False)
    daily["prev_high_250"] = groups["high"].transform(lambda s: s.shift(1).rolling(250, min_periods=120).max())
    daily["prev_high_60"] = groups["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=40).max())
    daily["ma50"] = groups["close"].transform(lambda s: moving_average(s, 50))
    daily["ma200"] = groups["close"].transform(lambda s: moving_average(s, 200))
    daily["amount_ma20"] = groups["amount"].transform(lambda s: moving_average(s, 20))

    daily["is_250_high"] = daily["prev_high_250"].gt(0) & daily["close"].gt(daily["prev_high_250"])
    daily["is_60_high"] = daily["prev_high_60"].gt(0) & daily["close"].gt(daily["prev_high_60"])
    daily["is_trend_middle"] = (
        daily["close"].gt(daily["ma50"])
        & daily["close"].gt(daily["ma200"])
        & daily["amount_ma20"].ge(config.MIN_AVG_AMOUNT_20D)
    )

    summary = daily.groupby(["industry", "trade_date"], as_index=False).agg(
        leader_250_high_count=("is_250_high", "sum"),
        leader_60_high_count=("is_60_high", "sum"),
        leader_trend_middle_count=("is_trend_middle", "sum"),
    )
    summary["leader_score"] = (
        summary["leader_250_high_count"].ge(1).astype(float) * 5.0
        + summary["leader_60_high_count"].ge(3).astype(float) * 5.0
        + summary["leader_trend_middle_count"].ge(1).astype(float) * 5.0
    )
    return summary[
        [
            "industry",
            "trade_date",
            "leader_score",
            "leader_250_high_count",
            "leader_60_high_count",
            "leader_trend_middle_count",
        ]
    ]


def score_industries(
    industry_daily: pd.DataFrame,
    trade_date: str,
    config: StrategyConfig,
    stock_daily: pd.DataFrame | None = None,
    stock_basic: pd.DataFrame | None = None,
) -> pd.DataFrame:
    leader_scores = _industry_leader_scores(stock_daily, stock_basic, config)
    frames = []
    for industry, group in industry_daily.groupby("industry"):
        group = group.sort_values("trade_date").copy()
        group["ret3"] = group["close"].pct_change(3) * 100
        group["ret5"] = group["close"].pct_change(5) * 100
        group["ret10"] = group["close"].pct_change(10) * 100
        group["ret20"] = group["close"].pct_change(20) * 100
        group["amount_ma20"] = moving_average(group["amount"], 20)
        member_count = (group["up_count"] + group["down_count"]).clip(lower=1)
        group["breadth"] = group["up_count"] / member_count
        group["breadth3"] = group["breadth"].rolling(3, min_periods=1).mean()
        group["limit_up_rate"] = group["limit_up_count"] / member_count
        group["persistence_score"] = (
            group["ret3"].apply(lambda x: pct_rank(float(x), -2, 5)) * 0.35
            + group["ret5"].apply(lambda x: pct_rank(float(x), -3, 8)) * 0.35
            + group["ret10"].apply(lambda x: pct_rank(float(x), -5, 12)) * 0.30
        ) * 0.25
        group["strength_score"] = (
            group["ret5"].apply(lambda x: pct_rank(float(x), -3, 10)) * 0.55
            + group["ret20"].apply(lambda x: pct_rank(float(x), -8, 18)) * 0.45
        ) * 0.20
        group["width_score"] = (
            group["breadth"].apply(lambda x: pct_rank(float(x), 0.45, 0.80)) * 0.65
            + group["breadth3"].apply(lambda x: pct_rank(float(x), 0.45, 0.75)) * 0.35
        ) * 0.15
        amount_ratio = group["amount"] / group["amount_ma20"]
        group["amount_ratio"] = amount_ratio.fillna(1.0)
        group["capacity_score"] = amount_ratio.apply(lambda x: pct_rank(float(x), 0.8, 1.8)) * 0.15
        group["hot_score"] = (
            group["limit_up_rate"].apply(lambda x: pct_rank(float(x), 0.0, 0.08)) * 0.60
            + group["pct_chg"].apply(lambda x: pct_rank(float(x), 0.0, 4.0)) * 0.40
        ) * 0.15
        group["logic_score"] = group["ret5"].apply(lambda x: pct_rank(float(x), 0.0, 8.0)) * 0.10
        if not leader_scores.empty:
            group = group.merge(
                leader_scores[leader_scores["industry"] == industry],
                on=["industry", "trade_date"],
                how="left",
            )
            group["leader_score"] = group["leader_score"].fillna(0.0)
            for detail_col in ("leader_250_high_count", "leader_60_high_count", "leader_trend_middle_count"):
                group[detail_col] = group[detail_col].fillna(0).astype(int)
        else:
            group["leader_score"] = 0.0
            group["leader_250_high_count"] = 0
            group["leader_60_high_count"] = 0
            group["leader_trend_middle_count"] = 0
        group["base_score"] = (
            group["persistence_score"]
            + group["strength_score"]
            + group["width_score"]
            + group["capacity_score"]
            + group["leader_score"]
        )
        group["total_score"] = group["base_score"] + group["logic_score"]
        group["score"] = group["total_score"]
        group["momentum_score"] = group["persistence_score"] + group["strength_score"]
        group["breadth_score"] = group["width_score"]
        group["amount_score"] = group["capacity_score"]
        frames.append(group)

    scored = pd.concat(frames, ignore_index=True)
    scored = scored.sort_values(["trade_date", "base_score"], ascending=[True, False]).copy()
    scored["rank"] = scored.groupby("trade_date").cumcount() + 1
    latest = scored[scored["trade_date"] == trade_date].copy()
    latest = latest.sort_values("base_score", ascending=False).reset_index(drop=True)

    candidate_map = {}
    confirmed_map = {}
    candidate_stability_map = {}
    confirmed_stability_map = {}
    strong_streak_map = {}
    rank_top5_avg_map = {}
    status_map = {}
    watch_map = {}
    near_map = {}
    downtrend_map = {}
    industry_count = max(1, int(latest["industry"].nunique()))
    top_rank_limit = max(1, math.ceil(industry_count * config.MAINLINE_WATCH_RANK_PCT))
    for industry, group in scored.groupby("industry"):
        ordered = group.sort_values("trade_date")
        tail3 = ordered.tail(config.MAINLINE_CONFIRMED_DAYS)
        tail5 = ordered.tail(5)
        latest_row = ordered.iloc[-1]
        recent3_candidate_hits = int(tail3["base_score"].ge(config.MAINLINE_CANDIDATE_SCORE).sum())
        latest_score = float(latest_row["base_score"])
        latest_rank = int(latest_row["rank"])
        latest_amount_ratio = float(latest_row.get("amount_ratio", 1.0) or 1.0)
        candidate_map[industry] = bool(
            (latest_score >= config.MAINLINE_CANDIDATE_SCORE and recent3_candidate_hits >= config.MAINLINE_CANDIDATE_DAYS)
            or (
                latest_score >= config.MAINLINE_CANDIDATE_SCORE
                and len(tail3) >= 2
                and tail3["base_score"].tail(2).ge(config.MAINLINE_WATCH_SCORE).all()
            )
        )
        confirmed_map[industry] = bool(
            (len(tail3) >= config.MAINLINE_CONFIRMED_DAYS and tail3["base_score"].ge(config.MAINLINE_CONFIRMED_SCORE).all())
            or tail5["base_score"].ge(config.MAINLINE_CONFIRMED_SCORE).sum() >= 3
        )
        candidate_stability_days = 0
        for val in reversed(ordered["base_score"].tolist()):
            if val >= config.MAINLINE_CANDIDATE_SCORE:
                candidate_stability_days += 1
            else:
                break
        confirmed_stability_days = 0
        for val in reversed(ordered["base_score"].tolist()):
            if val >= config.MAINLINE_CONFIRMED_SCORE:
                confirmed_stability_days += 1
            else:
                break
        candidate_stability_map[industry] = candidate_stability_days
        confirmed_stability_map[industry] = confirmed_stability_days
        strong_streak_days = 0
        for val in reversed(ordered["base_score"].tolist()):
            if val >= config.MAINLINE_WATCH_SCORE:
                strong_streak_days += 1
            else:
                break
        strong_streak_map[industry] = strong_streak_days
        rank_top5_avg_map[industry] = round(float(tail5["rank"].mean()), 2) if len(tail5) else float(latest_rank)
        classified = _classify_mainline_status(
            latest_row=latest_row,
            tail3=tail3,
            tail5=tail5,
            top_rank_limit=top_rank_limit,
            is_candidate=candidate_map[industry],
            is_confirmed=confirmed_map[industry],
            candidate_stability_days=candidate_stability_days,
            confirmed_stability_days=confirmed_stability_days,
            config=config,
        )
        status = classified["mainline_status"]
        near_confirm = bool(classified["is_near_confirm"])
        watch_mainline = bool(classified["is_watch_mainline"])
        downtrend_watch = bool(classified["is_downtrend_watch"])
        status_map[industry] = status
        watch_map[industry] = int(watch_mainline)
        near_map[industry] = int(near_confirm)
        downtrend_map[industry] = int(downtrend_watch)

    latest["is_candidate_mainline"] = latest["industry"].map(candidate_map).astype(int)
    latest["confirmed"] = latest["industry"].map(confirmed_map).astype(int)
    latest["candidate_stability_days"] = latest["industry"].map(candidate_stability_map).astype(int)
    latest["confirmed_stability_days"] = latest["industry"].map(confirmed_stability_map).astype(int)
    latest["strong_streak_days"] = latest["industry"].map(strong_streak_map).astype(int)
    latest["rank_top5_avg"] = latest["industry"].map(rank_top5_avg_map).astype(float)
    latest["mainline_status"] = latest["industry"].map(status_map).astype(str)
    latest["is_watch_mainline"] = latest["industry"].map(watch_map).astype(int)
    latest["is_near_confirm"] = latest["industry"].map(near_map).astype(int)
    latest["is_downtrend_watch"] = latest["industry"].map(downtrend_map).astype(int)
    latest["stability_days"] = latest["confirmed_stability_days"]
    latest["score"] = latest["total_score"]  # backward compat
    latest["rank_change"] = 0
    latest["score_change"] = 0.0
    latest["drift_flag"] = 0
    latest["drift_status"] = "stable"

    return latest[
        [
            "trade_date",
            "industry",
            "rank",
            "score",  # backward compat, = total_score
            "total_score",
            "base_score",
            "momentum_score",
            "breadth_score",
            "amount_score",
            "persistence_score",
            "strength_score",
            "width_score",
            "capacity_score",
            "hot_score",
            "leader_score",
            "leader_250_high_count",
            "leader_60_high_count",
            "leader_trend_middle_count",
            "logic_score",
            "amount_ratio",
            "mainline_status",
            "strong_streak_days",
            "rank_top5_avg",
            "is_watch_mainline",
            "is_near_confirm",
            "is_downtrend_watch",
            "confirmed",
            "is_candidate_mainline",
            "candidate_stability_days",
            "confirmed_stability_days",
            "stability_days",
            "rank_change",
            "score_change",
            "drift_flag",
            "drift_status",
        ]
    ].round(2)


def _classify_mainline_status(
    latest_row: pd.Series,
    tail3: pd.DataFrame,
    tail5: pd.DataFrame,
    top_rank_limit: int,
    is_candidate: bool,
    is_confirmed: bool,
    candidate_stability_days: int,
    confirmed_stability_days: int,
    config: StrategyConfig,
) -> dict[str, object]:
    latest_score = float(latest_row.get("base_score", 0) or 0)
    latest_rank = int(latest_row.get("rank", 9999) or 9999)
    latest_amount_ratio = float(latest_row.get("amount_ratio", 1.0) or 1.0)
    recent3_candidate_hits = int(tail3["base_score"].ge(config.MAINLINE_CANDIDATE_SCORE).sum()) if not tail3.empty else 0
    near_confirm = bool(
        config.MAINLINE_NEAR_CONFIRM_SCORE <= latest_score < config.MAINLINE_CONFIRMED_SCORE
        and recent3_candidate_hits >= config.MAINLINE_CANDIDATE_DAYS
        and latest_rank <= top_rank_limit
    )
    watch_mainline = bool(
        (
            latest_score >= config.MAINLINE_WATCH_SCORE
            and latest_rank <= top_rank_limit
            and latest_amount_ratio >= config.MAINLINE_WATCH_AMOUNT_RATIO
        )
        or (
            latest_score >= config.MAINLINE_WATCH_SCORE
            and float(latest_row.get("strength_score", 0) or 0) >= 15
            and float(latest_row.get("capacity_score", 0) or 0) >= 10
            and float(latest_row.get("leader_score", 0) or 0) >= 5
        )
    )
    downgrade_tail = tail3["base_score"].tail(config.MAINLINE_DOWNGRADE_DAYS) if not tail3.empty else pd.Series(dtype=float)
    downtrend_watch = bool(
        len(downgrade_tail) >= config.MAINLINE_DOWNGRADE_DAYS
        and downgrade_tail.lt(config.MAINLINE_DOWNGRADE_SCORE).all()
        and (
            tail5["base_score"].ge(config.MAINLINE_CANDIDATE_SCORE).any()
            or candidate_stability_days > 0
            or confirmed_stability_days > 0
            or is_candidate
            or is_confirmed
        )
    )
    status = "普通"
    if downtrend_watch:
        status = "退潮观察"
    elif is_confirmed:
        status = "确认主线"
    elif near_confirm:
        status = "接近确认"
    elif is_candidate:
        status = "候选主线"
    elif watch_mainline:
        status = "主线预警"
    return {
        "mainline_status": status,
        "is_watch_mainline": int(watch_mainline),
        "is_near_confirm": int(near_confirm),
        "is_downtrend_watch": int(downtrend_watch),
    }


def add_mainline_state(conn: sqlite3.Connection, industry_scores: pd.DataFrame) -> pd.DataFrame:
    previous = read_sql(
        conn,
        """
        SELECT industry, rank, base_score, mainline_status
        FROM industry_score
        WHERE batch_id = (SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1)
        """,
    )
    if previous.empty:
        return industry_scores

    previous_map = previous.set_index("industry").to_dict("index")
    previous_top3 = set(previous[previous["rank"] <= 3]["industry"].astype(str))
    for idx, row in industry_scores.iterrows():
        prev = previous_map.get(row["industry"])
        if not prev:
            continue
        rank_change = int(row["rank"] - int(prev["rank"]))
        score_change = float(row["base_score"] - float(prev["base_score"]))
        drift_flag = int(row["industry"] in previous_top3 and int(row["rank"]) > 10 and score_change < -20)
        industry_scores.loc[idx, "rank_change"] = rank_change
        industry_scores.loc[idx, "score_change"] = round(score_change, 2)
        industry_scores.loc[idx, "drift_flag"] = drift_flag
        if drift_flag:
            industry_scores.loc[idx, "drift_status"] = "drift_warning"
            if str(prev.get("mainline_status", "")) in {"候选主线", "接近确认", "确认主线"}:
                industry_scores.loc[idx, "mainline_status"] = "退潮观察"
                industry_scores.loc[idx, "is_downtrend_watch"] = 1
        elif rank_change <= -3:
            industry_scores.loc[idx, "drift_status"] = "rank_up"
        elif score_change <= -15:
            industry_scores.loc[idx, "drift_status"] = "score_drop_warning"
    return industry_scores


def select_stocks(
    data: dict[str, pd.DataFrame],
    trade_date: str,
    market: dict[str, float | str],
    industry_scores: pd.DataFrame,
    config: StrategyConfig,
    progress_callback: ProgressCallback | None = None,
) -> pd.DataFrame:
    basics = data["stock_basic"]
    daily = data["stock_daily"]
    financials = data["financials"].sort_values("report_date").groupby("code").tail(1)
    daily_groups = {code: group.sort_values("trade_date").copy() for code, group in daily.groupby("code")}
    finance_groups = {code: group for code, group in financials.groupby("code")}
    industry_map = industry_scores.set_index("industry").to_dict("index")
    rows: list[dict] = []

    total = len(basics)
    for idx, (_, stock) in enumerate(basics.iterrows(), start=1):
        if idx == 1 or idx % 100 == 0 or idx == total:
            _emit(progress_callback, f"逐股筛选：{idx}/{total}", 42 + int(idx / max(total, 1) * 48), 100)
        code = stock["code"]
        stock_daily = daily_groups.get(code, pd.DataFrame())
        finance_row = finance_groups.get(code, pd.DataFrame())
        fund_status = _fundamental_status(stock, finance_row, stock_daily, trade_date, config)
        if len(stock_daily) < config.MIN_HISTORY_DAYS:
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    "历史行情不足，无法计算趋势模板",
                    fundamental_status=fund_status,
                    rejected_reason_detail="趋势模板不通过",
                )
            )
            continue
        finance_reason = _fundamental_exclude_reason(stock, finance_row, stock_daily, trade_date, config)
        if finance_reason:
            finance_detail = _reason_detail_from_text(finance_reason)
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    finance_reason,
                    fundamental_status=fund_status,
                    rejected_reason_detail=finance_detail,
                )
            )
            continue
        trend_type, trend_reason = _trend_template_type(stock_daily)
        if trend_type == "C":
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    trend_reason,
                    trend_template_type=trend_type,
                    fundamental_status=fund_status,
                    rejected_reason_detail="趋势模板不通过",
                )
            )
            continue

        industry_state = industry_map.get(stock["industry"], {})
        mainline_stage = _mainline_trade_stage(industry_state) if industry_state else "普通"
        mainline_base_score = float(industry_state.get("base_score", industry_state.get("score", 0)) or 0) if industry_state else 0.0
        nearest_keypoint = _nearest_keypoint(stock_daily)
        keypoint_distance_pct = nearest_keypoint["keypoint_distance_pct"]
        market_score = float(market["total_score"])

        keypoint = _detect_keypoint(stock_daily, trend_type, config)
        if keypoint is None:
            keypoint = _detect_recent_keypoint(stock_daily, trend_type, config)
        if keypoint is None:
            keypoint_detail = _keypoint_reject_detail(stock_daily, trend_type, config)
            if _warning_pool_eligible(stock_daily, trend_type, industry_state, config):
                plan = _observation_pool_plan(
                    "预警个股池",
                    watch_price=nearest_keypoint["watch_price"],
                    trigger_price=nearest_keypoint["nearest_keypoint_price"],
                )
                rows.append(
                    _candidate_row(
                        stock=stock,
                        market=market,
                        industry_state=industry_state,
                        trend_type=trend_type,
                        fund_status=fund_status,
                        candidate_layer="预警个股池",
                        status="excluded",
                        plan=plan,
                        keypoint=None,
                        nearest_keypoint=nearest_keypoint,
                        include_reason="；".join(
                            [
                                "通过P0基本面硬过滤",
                                f"趋势模板{trend_type}类",
                                f"所属行业状态：{mainline_stage}",
                                "主线预警方向，提前加入观察",
                            ]
                        ),
                        exclude_reason="主线预警阶段，未形成有效关键点突破，不进入正式候选",
                        rejected_reason_detail=keypoint_detail,
                    )
                )
                continue
            if _focus_pool_eligible(stock_daily, trend_type, industry_state, config):
                plan = _observation_pool_plan(
                    "重点观察个股池",
                    watch_price=nearest_keypoint["watch_price"],
                    trigger_price=nearest_keypoint["nearest_keypoint_price"],
                )
                rows.append(
                    _candidate_row(
                        stock=stock,
                        market=market,
                        industry_state=industry_state,
                        trend_type=trend_type,
                        fund_status=fund_status,
                        candidate_layer="重点观察个股池",
                        status="excluded",
                        plan=plan,
                        keypoint=None,
                        nearest_keypoint=nearest_keypoint,
                        include_reason="；".join(
                            [
                                "通过P0基本面硬过滤",
                                f"趋势模板{trend_type}类",
                                f"所属行业状态：{mainline_stage}",
                                "接近确认主线方向，等待关键点触发",
                            ]
                        ),
                        exclude_reason="重点观察阶段，等待关键点触发或回踩确认",
                        rejected_reason_detail=keypoint_detail,
                    )
                )
                continue
            missing_count = 1
            if not industry_state or mainline_stage == "普通":
                missing_count += 1
            if market_score < config.MARKET_TRADE_SCORE:
                missing_count += 1
            candidate_layer = "接近候选" if missing_count == 1 else "剔除"
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    "未形成有效关键点突破",
                    trend_template_type=trend_type,
                    fundamental_status=fund_status,
                    candidate_layer=candidate_layer,
                    rejected_reason_detail=keypoint_detail,
                    industry_score=mainline_base_score,
                    mainline_status=mainline_stage,
                    mainline_base_score=mainline_base_score,
                    keypoint_distance_pct=keypoint_distance_pct,
                )
            )
            continue
        if not industry_state:
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    "行业评分缺失",
                    trend_template_type=trend_type,
                    fundamental_status=fund_status,
                    candidate_layer="技术突破候选",
                    rejected_reason_detail="行业强度不足",
                    keypoint=keypoint,
                    keypoint_distance_pct=_keypoint_distance(stock_daily, keypoint),
                )
            )
            continue
        if mainline_stage in {"普通", "退潮观察"}:
            missing_count = 1 + (1 if float(market["total_score"]) < config.MARKET_TRADE_SCORE else 0)
            candidate_layer = "接近候选" if missing_count == 1 else "技术突破候选"
            reason = "所属行业进入退潮观察，不生成新的买入计划" if mainline_stage == "退潮观察" else "所属行业未进入主线观察层级"
            rows.append(
                _excluded_row(
                    stock,
                    market,
                    reason,
                    trend_template_type=trend_type,
                    fundamental_status=fund_status,
                    candidate_layer=candidate_layer,
                    rejected_reason_detail="主线未确认" if mainline_stage == "普通" else "退潮观察",
                    industry_score=float(industry_state.get("base_score", industry_state.get("score", 0))),
                    keypoint=keypoint,
                    mainline_status=mainline_stage,
                    mainline_base_score=mainline_base_score,
                    keypoint_distance_pct=_keypoint_distance(stock_daily, keypoint),
                )
            )
            continue

        latest = stock_daily.iloc[-1]
        plan = _build_trade_plan(
            latest=latest,
            keypoint=keypoint,
            market=market,
            industry_state=industry_state,
            trend_type=trend_type,
            config=config,
            stock_daily=stock_daily,
        )
        include_reason = "；".join(
            [
                "通过P0基本面硬过滤",
                f"趋势模板{trend_type}类",
                f"出现{keypoint['keypoint_type']}",
                f"量能质量：{keypoint['volume_quality']}",
                f"收盘质量：{keypoint['close_quality']}",
                f"所属行业状态：{mainline_stage}",
            ]
        )
        if market_score < config.MARKET_TRADE_SCORE:
            candidate_layer = "重点观察个股池" if market_score >= config.MARKET_MIN_SCORE else "技术突破候选"
            plan = _observation_pool_plan(
                candidate_layer,
                watch_price=round(float(latest["close"]), 2),
                trigger_price=round(float(keypoint["keypoint_price"]), 2),
                suggested_action="重点观察，等待主线确认"
                if candidate_layer == "重点观察个股池"
                else "技术突破，等待市场或主线确认",
            )
            status = "excluded"
            exclude_reason = "市场评分不足"
            rejected_reason_detail = "市场评分不足"
        elif mainline_stage == "确认主线":
            if plan["suggested_action"] in {"建议试错买入", "建议计划买入"} and float(plan["suggested_position"] or 0) > 0:
                candidate_layer = "正式候选股池"
                status = "included"
                exclude_reason = ""
                rejected_reason_detail = ""
            else:
                candidate_layer = "重点观察个股池"
                plan = _observation_pool_plan(
                    candidate_layer,
                    watch_price=round(float(latest["close"]), 2),
                    trigger_price=round(float(keypoint["keypoint_price"]), 2),
                    suggested_action="重点观察，等待回踩确认",
                )
                status = "excluded"
                exclude_reason = "确认主线内个股等待回踩确认"
                rejected_reason_detail = "等待回踩确认"
        elif mainline_stage == "接近确认":
            candidate_layer = "重点观察个股池"
            plan = _observation_pool_plan(
                candidate_layer,
                watch_price=round(float(latest["close"]), 2),
                trigger_price=round(float(keypoint["keypoint_price"]), 2),
                suggested_action="重点观察，等待主线确认",
            )
            status = "excluded"
            exclude_reason = "主线接近确认但未达到确认主线标准"
            rejected_reason_detail = "主线未确认"
        elif mainline_stage in {"主线预警", "候选主线"}:
            candidate_layer = "预警个股池" if mainline_stage == "主线预警" else "重点观察个股池"
            plan = _observation_pool_plan(
                candidate_layer,
                watch_price=round(float(latest["close"]), 2),
                trigger_price=round(float(keypoint["keypoint_price"]), 2),
                suggested_action="主线预警，加入观察"
                if candidate_layer == "预警个股池"
                else "重点观察，等待主线确认",
            )
            status = "excluded"
            exclude_reason = f"{mainline_stage}阶段，仅观察或小仓试错，不进入正式候选"
            rejected_reason_detail = "主线未确认"
        else:
            candidate_layer = "技术突破候选"
            plan = _observation_pool_plan(
                candidate_layer,
                watch_price=round(float(latest["close"]), 2),
                trigger_price=round(float(keypoint["keypoint_price"]), 2),
            )
            status = "excluded"
            exclude_reason = "主线状态不允许新买入"
            rejected_reason_detail = "主线未确认"
        rows.append(
            _candidate_row(
                stock=stock,
                market=market,
                industry_state=industry_state,
                trend_type=trend_type,
                fund_status=fund_status,
                candidate_layer=candidate_layer,
                status=status,
                plan=plan,
                keypoint=keypoint,
                nearest_keypoint=nearest_keypoint,
                include_reason=include_reason,
                exclude_reason=exclude_reason,
                rejected_reason_detail=rejected_reason_detail,
            )
        )
    return pd.DataFrame(rows)


def _candidate_row(
    stock: pd.Series,
    market: dict[str, float | str],
    industry_state: dict[str, object],
    trend_type: str,
    fund_status: str,
    candidate_layer: str,
    status: str,
    plan: dict[str, float | str | None],
    keypoint: dict[str, str | float] | None,
    nearest_keypoint: dict[str, float | None],
    include_reason: str,
    exclude_reason: str,
    rejected_reason_detail: str,
) -> dict[str, object]:
    keypoint = keypoint or {}
    mainline_status = _mainline_trade_stage(industry_state) if industry_state else "普通"
    mainline_base_score = float(industry_state.get("base_score", industry_state.get("score", 0)) or 0) if industry_state else 0.0
    return {
        "code": stock["code"],
        "name": stock["name"],
        "industry": stock["industry"],
        "status": status,
        "candidate_layer": candidate_layer,
        "mainline_status": mainline_status,
        "mainline_base_score": mainline_base_score,
        "keypoint_distance_pct": _coerce_float(
            nearest_keypoint.get("keypoint_distance_pct")
            if nearest_keypoint.get("keypoint_distance_pct") is not None
            else keypoint.get("keypoint_distance_pct")
        ),
        "signal_status": plan["signal_status"],
        "market_score": float(market["total_score"]),
        "industry_score": mainline_base_score,
        "fundamental_status": fund_status,
        "trend_template_type": trend_type,
        "volume_quality": keypoint.get("volume_quality", ""),
        "close_quality": keypoint.get("close_quality", ""),
        "risk_level": str(market["risk_level"]),
        "trade_plan_type": plan["trade_plan_type"],
        "suggested_action": plan["suggested_action"],
        "keypoint_date": keypoint.get("keypoint_date"),
        "keypoint_price": keypoint.get("keypoint_price"),
        "keypoint_type": keypoint.get("keypoint_type"),
        "breakout_date": keypoint.get("breakout_date"),
        "breakout_close": keypoint.get("breakout_close"),
        "breakout_day_low": keypoint.get("breakout_day_low"),
        "breakout_ma10": keypoint.get("breakout_ma10"),
        **plan,
        "include_reason": include_reason,
        "exclude_reason": exclude_reason,
        "rejected_reason_detail": rejected_reason_detail,
        "risk_warning": RISK_WARNING,
    }


def _nearest_keypoint(stock_daily: pd.DataFrame) -> dict[str, float | None]:
    df = stock_daily.sort_values("trade_date").copy()
    if len(df) < 121:
        close = float(df.iloc[-1]["close"]) if not df.empty else 0.0
        return {"watch_price": round(close, 2) if close else None, "nearest_keypoint_price": None, "keypoint_distance_pct": None}
    latest = df.iloc[-1]
    previous = df.iloc[:-1]
    close = float(latest["close"])
    levels = [
        float(previous["high"].max()),
        float(previous["high"].tail(250).max()),
        float(previous.tail(120)["high"].max()),
    ]
    nearest = max(level for level in levels if math.isfinite(level))
    distance = (nearest - close) / max(close, 0.01)
    return {
        "watch_price": round(close, 2),
        "nearest_keypoint_price": round(nearest, 2),
        "keypoint_distance_pct": round(distance, 4),
    }


def _keypoint_distance(stock_daily: pd.DataFrame, keypoint: dict[str, str | float]) -> float | None:
    if stock_daily.empty or not keypoint:
        return None
    close = float(stock_daily.sort_values("trade_date").iloc[-1]["close"])
    key_price = float(keypoint["keypoint_price"])
    return round((key_price - close) / max(close, 0.01), 4)


def _warning_pool_eligible(
    stock_daily: pd.DataFrame,
    trend_type: str,
    industry_state: dict[str, object],
    config: StrategyConfig,
) -> bool:
    stage = _mainline_trade_stage(industry_state) if industry_state else "普通"
    if stage not in {"主线预警", "候选主线"}:
        return False
    if trend_type not in {"A", "B"}:
        return False
    context = _stock_strength_context(stock_daily)
    if not context:
        return False
    return (
        context["close"] > context["ma20"] > 0
        and context["close"] > context["ma50"] > 0
        and context["amount_ma20"] >= config.MIN_AVG_AMOUNT_20D
        and context["close_to_high250"] >= 0.75
        and context["volume_ma20_ratio"] < config.VOLUME_BREAKOUT_MAX_RATIO_20D
        and context["close_ma20_ratio"] < config.CLOSE_MA20_MAX_RATIO
        and context["close_high_ratio"] >= 0.96
    )


def _focus_pool_eligible(
    stock_daily: pd.DataFrame,
    trend_type: str,
    industry_state: dict[str, object],
    config: StrategyConfig,
) -> bool:
    stage = _mainline_trade_stage(industry_state) if industry_state else "普通"
    if stage != "接近确认":
        return False
    if trend_type not in {"A", "B"}:
        return False
    context = _stock_strength_context(stock_daily)
    nearest = _nearest_keypoint(stock_daily)
    distance = nearest.get("keypoint_distance_pct")
    near_keypoint = distance is not None and float(distance) <= 0.08
    return (
        context
        and context["amount_ma20"] >= config.MIN_AVG_AMOUNT_20D
        and (context["close"] > context["ma20"] > context["ma50"] or context["close"] > context["ma50"] > context["ma200"])
        and context["close_to_high250"] >= 0.75
        and context["volume_ma20_ratio"] < config.VOLUME_BREAKOUT_MAX_RATIO_20D
        and context["close_ma20_ratio"] < config.CLOSE_MA20_MAX_RATIO
        and context["close_high_ratio"] >= 0.96
        and near_keypoint
    )


def _stock_strength_context(stock_daily: pd.DataFrame) -> dict[str, float]:
    df = stock_daily.sort_values("trade_date").copy()
    if len(df) < 60:
        return {}
    df["ma20"] = moving_average(df["close"], 20)
    df["ma50"] = moving_average(df["close"], 50)
    df["ma200"] = moving_average(df["close"], 200)
    df["volume_ma20"] = moving_average(df["volume"], 20)
    df["amount_ma20"] = moving_average(df["amount"], 20)
    latest = df.iloc[-1]
    high250 = float(df["high"].tail(250).max())
    close = float(latest["close"])
    volume_ma20 = float(latest["volume_ma20"])
    return {
        "close": close,
        "high": float(latest["high"]),
        "ma20": float(latest["ma20"]),
        "ma50": float(latest["ma50"]),
        "ma200": float(latest["ma200"]) if not pd.isna(latest["ma200"]) else 0.0,
        "amount_ma20": float(latest["amount_ma20"]) if not pd.isna(latest["amount_ma20"]) else 0.0,
        "volume_ma20_ratio": float(latest["volume"] / volume_ma20) if volume_ma20 > 0 else 99.0,
        "close_ma20_ratio": float(close / latest["ma20"]) if float(latest["ma20"]) > 0 else 99.0,
        "close_high_ratio": float(close / latest["high"]) if float(latest["high"]) > 0 else 0.0,
        "close_to_high250": float(close / high250) if high250 > 0 else 0.0,
    }


def _observation_pool_plan(
    candidate_layer: str,
    watch_price: float | None,
    trigger_price: float | None,
    suggested_action: str | None = None,
) -> dict[str, float | str | None]:
    if candidate_layer == "预警个股池":
        action = suggested_action or "主线预警，加入观察"
        signal_status = "主线预警"
        trailing = "主线尚未确认，仅观察强度连续性，不生成正式买入区间"
    elif candidate_layer == "重点观察个股池":
        action = suggested_action or "重点观察，等待触发"
        signal_status = "重点观察"
        trailing = "等待主线确认、关键点触发或回踩确认，不生成正式买入区间"
    elif candidate_layer == "技术突破候选":
        action = suggested_action or "技术突破，等待市场或主线确认"
        signal_status = "技术突破"
        trailing = "技术条件较强，但市场或主线条件不足，不生成正式买入区间"
    else:
        action = suggested_action or "接近条件，继续观察"
        signal_status = "接近候选"
        trailing = "距离正式候选仍缺少关键条件，不生成正式买入区间"
    return {
        "signal_status": signal_status,
        "trade_plan_type": "观察计划",
        "suggested_action": action,
        "watch_price": watch_price,
        "trigger_price": trigger_price,
        "buy_lower": None,
        "buy_upper": None,
        "suggested_buy_price": None,
        "stop_loss_price": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "trailing_stop_rule": trailing,
        "suggested_position": 0.0,
        "action": action,
        "buy_range_low": None,
        "buy_range_high": None,
        "stop_loss": None,
        "take_profit": None,
        "moving_take_profit_rule": trailing,
        "position_pct": 0.0,
    }


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _emit(callback: ProgressCallback | None, message: str, current: int, total: int) -> None:
    if callback:
        callback(message, current, total)


def _load_market_data(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    return {
        "stock_basic": read_sql(conn, "SELECT * FROM stock_basic"),
        "index_daily": read_sql(conn, "SELECT * FROM index_daily"),
        "stock_daily": read_sql(conn, "SELECT * FROM stock_daily"),
        "industry_daily": read_sql(conn, "SELECT * FROM industry_daily"),
        "financials": read_sql(conn, "SELECT * FROM financials"),
    }


def _current_data_source(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT data_source FROM data_snapshot WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return "unknown"
    if not row:
        return "unknown"
    return str(row["data_source"])


def _persist_run(
    conn: sqlite3.Connection,
    batch_id: str,
    run_timestamp: str,
    trade_date: str,
    market: dict[str, float | str],
    industry_scores: pd.DataFrame,
    results: pd.DataFrame,
) -> None:
    conn.execute(
        """
        INSERT INTO market_score (
            batch_id, run_timestamp, trade_date, index_trend_score, profit_effect_score,
            activity_score, sentiment_score, style_consistency_score, total_score, risk_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            run_timestamp,
            trade_date,
            market["index_trend_score"],
            market["profit_effect_score"],
            market["activity_score"],
            market["sentiment_score"],
            market["style_consistency_score"],
            market["total_score"],
            market["risk_level"],
        ),
    )
    industry_to_write = industry_scores.copy()
    industry_to_write.insert(0, "batch_id", batch_id)
    industry_to_write.insert(1, "run_timestamp", run_timestamp)
    industry_to_write.to_sql("industry_score", conn, if_exists="append", index=False)

    result_to_write = results.copy()
    result_to_write.insert(0, "batch_id", batch_id)
    result_to_write.insert(1, "run_timestamp", run_timestamp)
    result_to_write.insert(2, "trade_date", trade_date)
    result_to_write.to_sql("strategy_result", conn, if_exists="append", index=False)
    included = result_to_write[result_to_write["status"] == "included"]
    for _, row in included.iterrows():
        conn.execute(
            """
            INSERT INTO watch_pool (
                code, name, industry, source_batch_id, added_at, note,
                trade_plan_type, suggested_action, watch_price, trigger_price,
                buy_lower, buy_upper, suggested_buy_price, stop_loss_price,
                take_profit_1, take_profit_2, trailing_stop_rule,
                suggested_position, risk_warning
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                industry=excluded.industry,
                source_batch_id=excluded.source_batch_id,
                added_at=excluded.added_at,
                note=excluded.note,
                trade_plan_type=excluded.trade_plan_type,
                suggested_action=excluded.suggested_action,
                watch_price=excluded.watch_price,
                trigger_price=excluded.trigger_price,
                buy_lower=excluded.buy_lower,
                buy_upper=excluded.buy_upper,
                suggested_buy_price=excluded.suggested_buy_price,
                stop_loss_price=excluded.stop_loss_price,
                take_profit_1=excluded.take_profit_1,
                take_profit_2=excluded.take_profit_2,
                trailing_stop_rule=excluded.trailing_stop_rule,
                suggested_position=excluded.suggested_position,
                risk_warning=excluded.risk_warning
            """,
            (
                row["code"],
                row["name"],
                row["industry"],
                batch_id,
                run_timestamp,
                row["include_reason"],
                row["trade_plan_type"],
                row["suggested_action"],
                row["watch_price"],
                row["trigger_price"],
                row["buy_lower"],
                row["buy_upper"],
                row["suggested_buy_price"],
                row["stop_loss_price"],
                row["take_profit_1"],
                row["take_profit_2"],
                row["trailing_stop_rule"],
                row["suggested_position"],
                row["risk_warning"],
            ),
        )


def _fundamental_exclude_reason(
    stock: pd.Series,
    finance_row: pd.DataFrame,
    stock_daily: pd.DataFrame,
    trade_date: str,
    config: StrategyConfig,
) -> str:
    reasons = []
    if int(stock.get("is_st", 0)):
        reasons.append("ST或*ST股票")
    if int(stock.get("is_delist_risk", 0)):
        reasons.append("存在退市风险")
    if int(stock.get("is_suspended", 0)):
        reasons.append("停牌股票")
    latest = stock_daily.iloc[-1]
    if int(latest.get("is_suspended", 0)):
        reasons.append("当日停牌")
    if float(latest["close"]) < config.MIN_PRICE:
        reasons.append("股价低于最低价格")
    if _list_days(stock.get("list_date", ""), trade_date) < config.MIN_LIST_DAYS:
        reasons.append("上市时间不足250个交易日")
    amount_ma20 = float(stock_daily["amount"].tail(20).mean())
    if amount_ma20 < config.MIN_AVG_AMOUNT_20D:
        reasons.append("20日平均成交额不足")
    turnover_20 = stock_daily["turnover_rate"].tail(20)
    if turnover_20.gt(0).any() and float(turnover_20.mean()) < config.MIN_TURNOVER_20D:
        reasons.append("20日平均换手率不足")
    if finance_row.empty:
        reasons.append("财务数据缺失或代理字段不足")
    else:
        row = finance_row.iloc[-1]
        if not str(row.get("report_date", "")).strip():
            reasons.append("财务数据缺失或代理字段不足")
        else:
            if _financial_field_missing(row, "net_profit"):
                reasons.append("财务数据缺失或代理字段不足")
            elif float(row["net_profit"]) <= 0:
                reasons.append("最近一年净利润不为正")
            if _financial_field_missing(row, "deducted_net_profit"):
                reasons.append("财务数据缺失或代理字段不足")
            elif float(row["deducted_net_profit"]) <= 0:
                reasons.append("最近一年扣非净利润不为正")
            if float(row["revenue_yoy"]) <= config.MIN_REVENUE_YOY:
                reasons.append("营收同比增长低于-10%")
            if float(row["asset_liability_ratio"]) >= config.MAX_DEBT_RATIO:
                reasons.append("资产负债率不低于75%")
    return "；".join(reasons)


def _trend_template_type(stock_daily: pd.DataFrame) -> tuple[str, str]:
    df = stock_daily.copy()
    df["ma50"] = moving_average(df["close"], 50)
    df["ma150"] = moving_average(df["close"], 150)
    df["ma200"] = moving_average(df["close"], 200)
    last = df.iloc[-1]
    low_250 = df["low"].tail(250).min()
    high_250 = df["high"].tail(250).max()
    close = float(last["close"])
    checks_a = [
        close > float(last["ma50"]),
        close > float(last["ma150"]),
        close > float(last["ma200"]),
        float(last["ma50"]) > float(last["ma150"]),
        float(last["ma150"]) > float(last["ma200"]),
        float(last["ma200"]) > float(df["ma200"].iloc[-21]),
        close / float(high_250) >= 0.75,
        close / float(low_250) >= 1.30,
    ]
    if all(checks_a):
        return "A", "A类趋势股"
    checks_b = [
        close > float(last["ma50"]),
        close > float(last["ma200"]),
        float(last["ma50"]) > float(last["ma200"]),
        close / float(high_250) >= 0.75,
    ]
    if all(checks_b):
        return "B", "B类趋势股"
    return "C", "C类趋势股不进入默认候选池"


def _detect_keypoint(
    stock_daily: pd.DataFrame,
    trend_type: str,
    config: StrategyConfig,
) -> dict[str, str | float] | None:
    df = stock_daily.sort_values("trade_date").copy()
    return _detect_keypoint_at(df, trend_type, config, len(df) - 1)


def _detect_recent_keypoint(
    stock_daily: pd.DataFrame,
    trend_type: str,
    config: StrategyConfig,
    lookback_days: int = 6,
) -> dict[str, str | float] | None:
    df = stock_daily.sort_values("trade_date").copy()
    if df.empty:
        return None
    start = max(0, len(df) - lookback_days)
    for end_pos in range(len(df) - 1, start - 1, -1):
        keypoint = _detect_keypoint_at(df, trend_type, config, end_pos)
        if keypoint is not None:
            return keypoint
    return None


def _detect_keypoint_at(
    stock_daily: pd.DataFrame,
    trend_type: str,
    config: StrategyConfig,
    end_pos: int,
) -> dict[str, str | float] | None:
    df = stock_daily.sort_values("trade_date").copy().iloc[: end_pos + 1]
    if len(df) < 121:
        return None
    df["volume_ma5"] = moving_average(df["volume"], 5)
    df["volume_ma20"] = moving_average(df["volume"], 20)
    df["ma20"] = moving_average(df["close"], 20)
    df["ma10"] = moving_average(df["close"], 10)
    latest = df.iloc[-1]
    previous = df.iloc[:-1]
    if previous.empty:
        return None

    close = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])
    breakout_day_low = low
    keypoints: list[dict[str, str | float]] = []

    # Keypoint detection with corrected price
    prev_high_max = float(previous["high"].max())
    if close > prev_high_max:
        keypoints.append({
            "keypoint_date": latest["trade_date"],
            "keypoint_price": prev_high_max,  # breakout level = previous ALL-TIME high
            "keypoint_type": "历史新高",
        })
    prev_high_250 = float(previous["high"].tail(250).max())
    if close > prev_high_250:
        keypoints.append({
            "keypoint_date": latest["trade_date"],
            "keypoint_price": prev_high_250,  # breakout level = previous 250-day high
            "keypoint_type": "250日新高",
        })
    platform = previous.tail(120)
    if not platform.empty:
        platform_high = float(platform["high"].max())
        platform_low = float(platform["low"].min())
        platform_width = (platform_high - platform_low) / max(platform_high, 0.01)
        if platform_width <= 0.35 and close > platform_high:
            keypoints.append({
                "keypoint_date": latest["trade_date"],
                "keypoint_price": platform_high,  # breakout level = platform top
                "keypoint_type": "120日平台突破",
            })
    if not keypoints:
        return None

    volume = float(latest["volume"])
    volume_ma5 = float(latest["volume_ma5"])
    volume_ma20 = float(latest["volume_ma20"])
    volume_ok = (
        volume_ma5 > 0
        and volume_ma20 > 0
        and volume > volume_ma5 * config.VOLUME_BREAKOUT_MIN_RATIO_5D
        and volume > volume_ma20 * config.VOLUME_BREAKOUT_MIN_RATIO_20D
        and volume < volume_ma20 * config.VOLUME_BREAKOUT_MAX_RATIO_20D
    )
    if not volume_ok:
        return None

    ma20 = float(latest["ma20"])
    close_position = (close - low) / max(high - low, 0.01)
    close_ma20_limit = config.LEADER_CLOSE_MA20_MAX_RATIO if trend_type == "A" else config.CLOSE_MA20_MAX_RATIO
    close_ok = (
        close_position > 0.70
        and ma20 > 0
        and close / ma20 < close_ma20_limit
        and close / high >= config.CLOSE_HIGH_MIN_RATIO
    )
    if not close_ok:
        return None

    priority = {"历史新高": 3, "250日新高": 2, "120日平台突破": 1}
    selected = sorted(keypoints, key=lambda x: priority[str(x["keypoint_type"])], reverse=True)[0]
    selected["breakout_date"] = latest["trade_date"]
    selected["breakout_close"] = close
    selected["breakout_day_low"] = breakout_day_low
    selected["breakout_ma10"] = float(latest["ma10"])
    selected["volume_quality"] = (
        f"VOL/MA5={volume / volume_ma5:.2f}, VOL/MA20={volume / volume_ma20:.2f}"
    )
    selected["close_quality"] = (
        f"close_position={close_position:.2f}, close/high={close / high:.2f}, close/MA20={close / ma20:.2f}"
    )
    return selected


def _keypoint_reject_detail(
    stock_daily: pd.DataFrame,
    trend_type: str,
    config: StrategyConfig,
) -> str:
    df = stock_daily.sort_values("trade_date").copy()
    if len(df) < 121:
        return "未触发关键点"
    df["volume_ma5"] = moving_average(df["volume"], 5)
    df["volume_ma20"] = moving_average(df["volume"], 20)
    df["ma20"] = moving_average(df["close"], 20)
    latest = df.iloc[-1]
    previous = df.iloc[:-1]
    if previous.empty:
        return "未触发关键点"

    close = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])
    prev_high_max = float(previous["high"].max())
    prev_high_250 = float(previous["high"].tail(250).max())
    platform = previous.tail(120)
    platform_breakout = False
    if not platform.empty:
        platform_high = float(platform["high"].max())
        platform_low = float(platform["low"].min())
        platform_width = (platform_high - platform_low) / max(platform_high, 0.01)
        platform_breakout = platform_width <= 0.35 and close > platform_high
    if not (close > prev_high_max or close > prev_high_250 or platform_breakout):
        return "未触发关键点"

    volume = float(latest["volume"])
    volume_ma5 = float(latest["volume_ma5"])
    volume_ma20 = float(latest["volume_ma20"])
    if volume_ma5 <= 0 or volume_ma20 <= 0:
        return "成交量不足"
    if volume <= volume_ma5 * config.VOLUME_BREAKOUT_MIN_RATIO_5D or volume <= volume_ma20 * config.VOLUME_BREAKOUT_MIN_RATIO_20D:
        return "成交量不足"
    if volume >= volume_ma20 * config.VOLUME_BREAKOUT_MAX_RATIO_20D:
        return "成交量过大"

    ma20 = float(latest["ma20"])
    close_ma20_limit = config.LEADER_CLOSE_MA20_MAX_RATIO if trend_type == "A" else config.CLOSE_MA20_MAX_RATIO
    if ma20 <= 0 or close / ma20 >= close_ma20_limit:
        return "距离 MA20 过远"
    close_position = (close - low) / max(high - low, 0.01)
    if close_position <= 0.70 or close / high < config.CLOSE_HIGH_MIN_RATIO:
        return "收盘质量不足"
    return "未触发关键点"


def _build_trade_plan(
    latest: pd.Series,
    keypoint: dict[str, str | float],
    market: dict[str, float | str],
    industry_state: dict[str, object],
    trend_type: str,
    config: StrategyConfig,
    stock_daily: pd.DataFrame | None = None,
) -> dict[str, float | str | None]:
    key_price = float(keypoint["keypoint_price"])
    breakout_close = float(keypoint.get("breakout_close", key_price))
    breakout_day_low = float(keypoint.get("breakout_day_low", key_price * 0.97))
    breakout_ma10 = float(keypoint.get("breakout_ma10", key_price * 0.96))
    market_score = float(market["total_score"])
    industry_score = float(industry_state.get("base_score", industry_state.get("score", 0)))
    mainline_status = _mainline_trade_stage(industry_state)
    watch_price = round(key_price, 2)
    trigger_price = round(key_price * 1.02, 2)

    latest_date = str(latest.get("trade_date", ""))
    breakout_date = str(keypoint.get("breakout_date") or keypoint.get("keypoint_date"))
    is_breakout_today = breakout_date == latest_date
    is_leader = trend_type == "A" and is_breakout_today

    # ── market_score < 65: no formal buy range ──
    if market_score < config.MARKET_TRADE_SCORE:
        return _plan_only_watch(watch_price, trigger_price, is_leader)
    if mainline_status == "退潮观察":
        return _plan_no_new_buy(watch_price, trigger_price, "退潮观察阶段，不生成新的买入计划")
    if mainline_status == "普通":
        return _plan_no_new_buy(watch_price, trigger_price, "普通行业，不作为主线重点观察方向")

    if mainline_status == "主线预警":
        if _is_watch_leader_trial(industry_state, trend_type, keypoint, config, is_breakout_today):
            return _leader_breakout_plan(
                key_price,
                breakout_close,
                breakout_day_low,
                breakout_ma10,
                market_score,
                config,
                suggested_action="主线预警，龙头试错",
                signal_status="主线预警龙头",
                position_override=3.0 if market_score < 75 else 5.0,
            )
        return _plan_no_new_buy(watch_price, trigger_price, "主线预警阶段仅允许A类强关键点龙头小仓试错")

    if mainline_status in {"候选主线", "接近确认"}:
        if is_leader:
            return _leader_breakout_plan(
                key_price,
                breakout_close,
                breakout_day_low,
                breakout_ma10,
                market_score,
                config,
                suggested_action="龙头突破试错",
                signal_status=mainline_status,
                position_override=5.0 if market_score < 75 else 10.0,
            )
        return _plan_no_new_buy(watch_price, trigger_price, f"{mainline_status}阶段，中军等待回踩确认，不追后排")

    # ── Leader Breakout Trial ──
    if mainline_status == "确认主线" and is_leader:
        return _leader_breakout_plan(key_price, breakout_close, breakout_day_low, breakout_ma10, market_score, config)

    # ── Pullback Confirmation Check ──
    if mainline_status == "确认主线" and stock_daily is not None:
        pullback = _detect_pullback(stock_daily, keypoint, config)
        if pullback is not None:
            return _pullback_confirm_plan(key_price, pullback, market_score, config)

    # ── No pullback confirmation: waiting ──
    return {
        "signal_status": "等待回踩",
        "trade_plan_type": "中军回踩确认计划",
        "suggested_action": "等待回踩",
        "watch_price": watch_price,
        "trigger_price": trigger_price,
        "buy_lower": None,
        "buy_upper": None,
        "suggested_buy_price": None,
        "stop_loss_price": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "trailing_stop_rule": "等待突破后1-5日缩量回踩确认",
        "suggested_position": 0.0,
        "action": "等待回踩",
        "buy_range_low": None,
        "buy_range_high": None,
        "stop_loss": None,
        "take_profit": None,
        "moving_take_profit_rule": "等待突破后1-5日缩量回踩确认",
        "position_pct": 0.0,
    }


def _plan_only_watch(watch_price: float, trigger_price: float, is_leader: bool) -> dict:
    plan_type = "龙头突破试错计划" if is_leader else "中军回踩确认计划"
    return {
        "signal_status": "重点跟踪",
        "trade_plan_type": plan_type,
        "suggested_action": "仅观察",
        "watch_price": watch_price,
        "trigger_price": trigger_price,
        "buy_lower": None,
        "buy_upper": None,
        "suggested_buy_price": None,
        "stop_loss_price": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "trailing_stop_rule": "市场评分低于65，不生成正式买入区间",
        "suggested_position": 0.0,
        "action": "仅观察",
        "buy_range_low": None,
        "buy_range_high": None,
        "stop_loss": None,
        "take_profit": None,
        "moving_take_profit_rule": "市场评分低于65，不生成正式买入区间",
        "position_pct": 0.0,
    }


def _mainline_trade_stage(industry_state: dict[str, object] | pd.Series) -> str:
    status = str(industry_state.get("mainline_status", "") or "")
    if status:
        return status
    if int(industry_state.get("confirmed", 0) or 0) == 1:
        return "确认主线"
    if int(industry_state.get("is_near_confirm", 0) or 0) == 1:
        return "接近确认"
    if int(industry_state.get("is_candidate_mainline", 0) or 0) == 1:
        return "候选主线"
    if int(industry_state.get("is_watch_mainline", 0) or 0) == 1:
        return "主线预警"
    if int(industry_state.get("is_downtrend_watch", 0) or 0) == 1:
        return "退潮观察"
    return "普通"


def _is_watch_leader_trial(
    industry_state: dict[str, object],
    trend_type: str,
    keypoint: dict[str, str | float],
    config: StrategyConfig,
    is_breakout_today: bool,
) -> bool:
    if trend_type != "A" or not is_breakout_today:
        return False
    if str(keypoint.get("keypoint_type", "")) not in {"历史新高", "250日新高"}:
        return False
    rank = int(industry_state.get("rank", 9999) or 9999)
    # score_industries has already converted rank pct into is_watch_mainline; keep
    # this as a final local guard for direct unit calls.
    if rank > max(1, int(industry_state.get("watch_rank_limit", rank) or rank)):
        return int(industry_state.get("is_watch_mainline", 0) or 0) == 1
    return float(industry_state.get("base_score", 0) or 0) >= config.MAINLINE_WATCH_SCORE


def _plan_no_new_buy(watch_price: float, trigger_price: float, reason: str) -> dict:
    return {
        "signal_status": "重点跟踪",
        "trade_plan_type": "观察计划",
        "suggested_action": "仅观察",
        "watch_price": watch_price,
        "trigger_price": trigger_price,
        "buy_lower": None,
        "buy_upper": None,
        "suggested_buy_price": None,
        "stop_loss_price": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "trailing_stop_rule": reason,
        "suggested_position": 0.0,
        "action": "仅观察",
        "buy_range_low": None,
        "buy_range_high": None,
        "stop_loss": None,
        "take_profit": None,
        "moving_take_profit_rule": reason,
        "position_pct": 0.0,
    }


def _leader_breakout_plan(
    key_price: float,
    breakout_close: float,
    breakout_day_low: float,
    breakout_ma10: float,
    market_score: float,
    config: StrategyConfig,
    suggested_action: str = "建议试错买入",
    signal_status: str = "建议试错买入",
    position_override: float | None = None,
) -> dict:
    buy_lower = round(key_price, 2)
    buy_upper = round(key_price * 1.02, 2)
    suggested_buy = round((buy_lower + buy_upper) / 2, 2)

    # Stop loss: max of three candidates, with guardrails
    stops = [key_price * 0.97, breakout_day_low]
    if breakout_ma10 < suggested_buy:
        stops.append(breakout_ma10)
    stop_loss_price = round(max(stops), 2)
    if stop_loss_price >= buy_lower:
        stop_loss_price = round(min(key_price * 0.97, breakout_day_low), 2)

    take_profit_1 = round(suggested_buy * 1.10, 2)
    take_profit_2 = round(suggested_buy * 1.20, 2)
    position = position_override if position_override is not None else (10.0 if market_score >= 75 else 5.0)
    trailing = (
        "盈利超10%后若收盘跌破MA5建议减仓；"
        "盈利超15%后若收盘跌破MA10建议止盈；"
        "高位放量长上影建议减仓；主线连续2日降级建议减仓"
    )
    return {
        "signal_status": signal_status,
        "trade_plan_type": "龙头突破试错计划",
        "suggested_action": suggested_action,
        "watch_price": round(key_price, 2),
        "trigger_price": round(key_price * 1.02, 2),
        "buy_lower": buy_lower,
        "buy_upper": buy_upper,
        "suggested_buy_price": suggested_buy,
        "stop_loss_price": stop_loss_price,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "trailing_stop_rule": trailing,
        "suggested_position": position,
        "action": suggested_action,
        "buy_range_low": buy_lower,
        "buy_range_high": buy_upper,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_1,
        "moving_take_profit_rule": trailing,
        "position_pct": position,
    }


def _detect_pullback(
    stock_daily: pd.DataFrame,
    keypoint: dict[str, str | float],
    config: StrategyConfig,
) -> dict | None:
    """Detect pullback confirmation after breakout (1-5 trading days)."""
    key_price = float(keypoint["keypoint_price"])
    breakout_date = str(keypoint.get("breakout_date") or keypoint.get("keypoint_date"))
    breakout_close = float(keypoint.get("breakout_close", key_price))
    df = stock_daily.sort_values("trade_date").copy()
    df["ma10"] = moving_average(df["close"], 10)
    df["ma20"] = moving_average(df["close"], 20)
    df["volume_ma5"] = moving_average(df["volume"], 5)
    df["volume_ma20"] = moving_average(df["volume"], 20)

    matches = df.index[df["trade_date"].astype(str) == breakout_date].tolist()
    if not matches:
        return None
    breakout_idx = int(matches[-1])

    confirm_idx = len(df) - 1
    days_after_breakout = confirm_idx - breakout_idx
    if days_after_breakout < 1 or days_after_breakout > 5:
        return None

    breakout_row = df.iloc[breakout_idx]
    confirm_row = df.iloc[confirm_idx]
    pullback_window = df.iloc[breakout_idx + 1:confirm_idx]
    single_day_confirmation = pullback_window.empty
    support_window = df.iloc[confirm_idx:confirm_idx + 1] if single_day_confirmation else pullback_window

    pullback_low = float(support_window["low"].min())
    weakest_row = support_window.loc[support_window["low"].idxmin()]
    ma10_support = float(weakest_row["ma10"])
    ma20_support = float(weakest_row["ma20"])
    if pullback_low < max(key_price, ma10_support, ma20_support):
        return None

    breakout_volume = float(breakout_row["volume"])
    if single_day_confirmation:
        pullback_volume_shrink = bool(float(confirm_row["volume"]) < breakout_volume)
    else:
        pullback_volume_shrink = bool(
            pullback_window["volume"].lt(breakout_volume).all()
            and pullback_window["volume"].lt(pullback_window["volume_ma5"]).any()
        )
    if not pullback_volume_shrink:
        return None

    confirm_volume = float(confirm_row["volume"])
    if single_day_confirmation:
        confirm_volume_expand = bool(confirm_row["close"] > confirm_row["open"])
    else:
        pullback_avg_volume = float(pullback_window["volume"].mean())
        confirm_volume_expand = bool(
            confirm_row["close"] > confirm_row["open"]
            and confirm_volume > pullback_avg_volume
            and confirm_volume > float(confirm_row["volume_ma5"])
        )
    if not confirm_volume_expand:
        return None

    return {
        "breakout_date": breakout_date,
        "breakout_close": breakout_close,
        "keypoint_price": key_price,
        "pullback_low": pullback_low,
        "pullback_volume_shrink": 1,
        "confirm_date": str(confirm_row["trade_date"]),
        "confirm_close": float(confirm_row["close"]),
        "confirm_volume_expand": 1,
        "ma20": float(confirm_row["ma20"]),
    }


def _pullback_confirm_plan(
    key_price: float,
    pullback: dict,
    market_score: float,
    config: StrategyConfig,
) -> dict:
    pullback_low = float(pullback["pullback_low"])
    confirm_close = float(pullback["confirm_close"])
    ma20 = float(pullback["ma20"])

    buy_lower = round(max(ma20, key_price * 0.98), 2)
    buy_upper = round(min(confirm_close * 1.02, key_price * 1.03), 2)

    if buy_lower > buy_upper:
        return {
            "signal_status": "等待回踩",
            "trade_plan_type": "中军回踩确认计划",
            "suggested_action": "等待回踩",
            "watch_price": round(key_price, 2),
            "trigger_price": round(key_price * 1.02, 2),
            "buy_lower": None,
            "buy_upper": None,
            "suggested_buy_price": None,
            "stop_loss_price": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "pullback_low": pullback_low,
            "confirm_date": pullback.get("confirm_date"),
            "confirm_close": confirm_close,
            "pullback_volume_shrink": pullback.get("pullback_volume_shrink"),
            "confirm_volume_expand": pullback.get("confirm_volume_expand"),
            "trailing_stop_rule": "买入区间无效(buy_lower>buy_upper)，等待更好的回踩",
            "suggested_position": 0.0,
            "action": "等待回踩",
            "buy_range_low": None,
            "buy_range_high": None,
            "stop_loss": None,
            "take_profit": None,
            "moving_take_profit_rule": "买入区间无效(buy_lower>buy_upper)，等待更好的回踩",
            "position_pct": 0.0,
        }

    suggested_buy = round((buy_lower + buy_upper) / 2, 2)

    # Stop loss: max of three candidates
    stops = [pullback_low, ma20, key_price * 0.97]
    stop_loss_price = round(max(stops), 2)
    if stop_loss_price >= buy_lower:
        stop_loss_price = round(min(pullback_low, ma20), 2)

    take_profit_1 = round(suggested_buy * 1.12, 2)
    take_profit_2 = round(suggested_buy * 1.25, 2)
    position = 15.0 if market_score >= 80 else 10.0
    trailing = (
        "盈利超12%后若收盘跌破MA10建议减仓；"
        "盈利超20%后若收盘跌破MA20建议止盈；"
        "高位放量长上影建议减仓；主线连续2日降级建议减仓；个股跌破MA20建议止损"
    )
    return {
        "signal_status": "回踩确认",
        "trade_plan_type": "中军回踩确认计划",
        "suggested_action": "建议计划买入",
        "watch_price": round(key_price, 2),
        "trigger_price": round(key_price * 1.02, 2),
        "buy_lower": buy_lower,
        "buy_upper": buy_upper,
        "suggested_buy_price": suggested_buy,
        "stop_loss_price": stop_loss_price,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "pullback_low": pullback_low,
        "confirm_date": pullback.get("confirm_date"),
        "confirm_close": confirm_close,
        "pullback_volume_shrink": pullback.get("pullback_volume_shrink"),
        "confirm_volume_expand": pullback.get("confirm_volume_expand"),
        "trailing_stop_rule": trailing,
        "suggested_position": position,
        "action": "建议计划买入",
        "buy_range_low": buy_lower,
        "buy_range_high": buy_upper,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_1,
        "moving_take_profit_rule": trailing,
        "position_pct": position,
    }


def _period_return(series: pd.Series, window: int) -> float:
    if len(series) <= window:
        return 0.0
    return float(series.iloc[-1] / series.iloc[-window - 1] - 1)


def _fundamental_status(
    stock: pd.Series,
    finance_row: pd.DataFrame,
    stock_daily: pd.DataFrame | None = None,
    trade_date: str | None = None,
    config: StrategyConfig | None = None,
) -> str:
    """Grade fundamental health: A=clean, B=minor, C=risky, D=excluded."""
    if int(stock.get("is_st", 0)) or int(stock.get("is_delist_risk", 0)) or int(stock.get("is_suspended", 0)):
        return "D"
    if stock_daily is not None and not stock_daily.empty and int(stock_daily.iloc[-1].get("is_suspended", 0)):
        return "D"
    if trade_date is not None and config is not None:
        if _list_days(stock.get("list_date", ""), trade_date) < config.MIN_LIST_DAYS:
            return "D"
    if finance_row.empty:
        return "C"
    row = finance_row.iloc[-1]
    if not str(row.get("report_date", "")).strip():
        return "C"
    if _financial_field_missing(row, "net_profit") or _financial_field_missing(row, "deducted_net_profit"):
        return "C"
    if float(row["net_profit"]) <= 0:
        return "D"
    if float(row["deducted_net_profit"]) <= 0:
        return "D"
    if float(row["revenue_yoy"]) <= -10:
        return "D"
    if float(row["asset_liability_ratio"]) >= 75:
        return "D"
    if float(row["revenue_yoy"]) < 0 or float(row["asset_liability_ratio"]) >= 65:
        return "B"
    return "A"


def _list_days(list_date: object, trade_date: str) -> int:
    try:
        start = pd.to_datetime(str(list_date), format="%Y%m%d", errors="coerce")
        if pd.isna(start):
            start = pd.to_datetime(str(list_date), errors="coerce")
        end = pd.to_datetime(trade_date)
        if pd.isna(start):
            return 9999
        return int((end - start).days)
    except Exception:
        return 9999


def _financial_field_missing(row: pd.Series, field: str) -> bool:
    missing_flag = f"{field}_missing"
    if missing_flag in row.index:
        return int(row.get(missing_flag, 0) or 0) == 1
    note = str(row.get("data_quality_note", "") or "")
    if field == "net_profit":
        return "归母净利润字段缺失" in note
    if field == "deducted_net_profit":
        return "扣非净利润字段缺失" in note
    return "财务数据缺失或代理字段不足" in note


def _excluded_row(
    stock: pd.Series,
    market: dict[str, float | str],
    reason: str,
    trend_template_type: str = "",
    fundamental_status: str = "C",
    keypoint: dict[str, str | float] | None = None,
    candidate_layer: str = "剔除",
    rejected_reason_detail: str = "",
    industry_score: float = 0.0,
    mainline_status: str = "",
    mainline_base_score: float = 0.0,
    keypoint_distance_pct: float | None = None,
) -> dict[str, object]:
    keypoint = keypoint or {}
    detail = rejected_reason_detail or _reason_detail_from_text(reason)
    return {
        "code": stock["code"],
        "name": stock["name"],
        "industry": stock["industry"],
        "status": "excluded",
        "candidate_layer": candidate_layer,
        "mainline_status": mainline_status,
        "mainline_base_score": mainline_base_score,
        "keypoint_distance_pct": keypoint_distance_pct,
        "signal_status": "",
        "market_score": float(market["total_score"]),
        "industry_score": industry_score,
        "fundamental_status": fundamental_status,
        "trend_template_type": trend_template_type,
        "volume_quality": keypoint.get("volume_quality", ""),
        "close_quality": keypoint.get("close_quality", ""),
        "risk_level": str(market["risk_level"]),
        "trade_plan_type": "",
        "suggested_action": None,
        "keypoint_date": keypoint.get("keypoint_date"),
        "keypoint_price": keypoint.get("keypoint_price"),
        "keypoint_type": keypoint.get("keypoint_type"),
        "breakout_date": keypoint.get("breakout_date"),
        "breakout_close": keypoint.get("breakout_close"),
        "breakout_day_low": keypoint.get("breakout_day_low"),
        "breakout_ma10": keypoint.get("breakout_ma10"),
        "pullback_low": None,
        "confirm_date": None,
        "confirm_close": None,
        "pullback_volume_shrink": None,
        "confirm_volume_expand": None,
        "action": None,
        "watch_price": None,
        "trigger_price": None,
        "buy_lower": None,
        "buy_upper": None,
        "suggested_buy_price": None,
        "stop_loss_price": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "trailing_stop_rule": None,
        "suggested_position": 0.0,
        "buy_range_low": None,
        "buy_range_high": None,
        "stop_loss": None,
        "take_profit": None,
        "moving_take_profit_rule": None,
        "position_pct": 0.0,
        "include_reason": "",
        "exclude_reason": reason,
        "rejected_reason_detail": detail,
        "risk_warning": RISK_WARNING,
    }


def _reason_detail_from_text(reason: str) -> str:
    if "财务数据缺失" in reason or "代理字段不足" in reason:
        return "财务数据缺失或代理字段不足"
    if any(token in reason for token in ["净利润", "扣非", "营收", "资产负债", "财务", "ST", "退市", "停牌", "上市时间", "成交额", "换手率", "股价"]):
        return "基本面不合格"
    if "趋势" in reason:
        return "趋势模板不通过"
    if "主线" in reason:
        return "主线未确认"
    if "行业" in reason:
        return "行业强度不足"
    if "关键点" in reason:
        return "未触发关键点"
    if "市场评分" in reason:
        return "市场评分不足"
    return reason

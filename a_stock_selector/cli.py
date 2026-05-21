from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT
from .data_provider import SampleDataProvider, TushareDataProvider, save_dataset, save_dataset_incremental
from .db import DEFAULT_DB_PATH, connect, init_db, table_count
from .strategy import run_strategy


CANDIDATE_EXPORT_COLUMNS = [
    "code",
    "name",
    "industry",
    "status",
    "candidate_layer",
    "mainline_status",
    "mainline_base_score",
    "signal_status",
    "market_score",
    "industry_score",
    "fundamental_status",
    "trend_template_type",
    "keypoint_type",
    "keypoint_price",
    "keypoint_distance_pct",
    "volume_quality",
    "close_quality",
    "risk_level",
    "trade_plan_type",
    "suggested_action",
    "watch_price",
    "trigger_price",
    "buy_lower",
    "buy_upper",
    "suggested_buy_price",
    "stop_loss_price",
    "take_profit_1",
    "take_profit_2",
    "trailing_stop_rule",
    "suggested_position",
    "include_reason",
    "exclude_reason",
    "rejected_reason_detail",
    "risk_warning",
]


def main() -> None:
    parser = argparse.ArgumentParser(prog="a-stock-selector")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create database and load sample data")
    sub.add_parser("fetch", help="Fetch full-market data from Tushare")
    run_parser = sub.add_parser("run", help="Run selector strategy")
    run_parser.add_argument("--refresh", action="store_true", help="Refresh data before running")
    sub.add_parser("observe", help="Export latest post-market observation record")
    args = parser.parse_args()

    init_db(args.db)
    with connect(args.db) as conn:
        if args.command == "init":
            dataset = SampleDataProvider().fetch()
            save_dataset(conn, dataset)
            print(f"initialized {args.db} with {table_count(conn, 'stock_basic')} stocks")
            return
        if args.command == "fetch":
            skip_dates = _reusable_trade_dates(conn) if _current_source(conn) == "tushare" else set()
            dataset = TushareDataProvider(max_stocks=0, skip_trade_dates=skip_dates).fetch()
            if skip_dates:
                save_dataset_incremental(conn, dataset)
            else:
                save_dataset(conn, dataset)
            print(f"fetched data_source={dataset.source_name}, stocks={table_count(conn, 'stock_basic')}")
            return
        if args.command == "run":
            if args.refresh or table_count(conn, "stock_daily") == 0:
                skip_dates = _reusable_trade_dates(conn) if _current_source(conn) == "tushare" else set()
                dataset = TushareDataProvider(max_stocks=0, skip_trade_dates=skip_dates).fetch()
                if skip_dates:
                    save_dataset_incremental(conn, dataset)
                else:
                    save_dataset(conn, dataset)
            row = conn.execute("SELECT data_source FROM data_snapshot WHERE id = 1").fetchone()
            if not row or row["data_source"] != "tushare":
                raise SystemExit("No Tushare dataset loaded. Run `a-stock-selector fetch` first.")
            summary = run_strategy(conn)
            print(
                "batch_id={batch_id} trade_date={trade_date} candidates={candidate_count} "
                "excluded={excluded_count}".format(**summary.__dict__)
            )
            return
        if args.command == "observe":
            observation = export_observation(conn)
            print(_format_observation_output(observation))
            return


def export_observation(conn, output_root: Path | None = None) -> dict[str, object]:
    output_root = output_root or (PROJECT_ROOT / "deliverables")
    observations_dir = output_root / "observations"
    observations_dir.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    run = conn.execute("SELECT * FROM run_log ORDER BY run_timestamp DESC LIMIT 1").fetchone()
    if not run:
        raise SystemExit("No strategy run found. Run `python -m a_stock_selector.cli run --refresh` first.")
    batch_id = str(run["batch_id"])
    trade_date = str(run["trade_date"])

    market = conn.execute("SELECT * FROM market_score WHERE batch_id = ?", (batch_id,)).fetchone()
    if not market:
        raise SystemExit(f"No market_score found for batch_id={batch_id}")

    top_mainlines = conn.execute(
        """
        SELECT rank, industry, base_score, total_score, mainline_status, confirmed, is_candidate_mainline,
               is_watch_mainline, is_near_confirm, is_downtrend_watch,
               candidate_stability_days, confirmed_stability_days, stability_days
        FROM industry_score
        WHERE batch_id = ?
        ORDER BY rank
        LIMIT 5
        """,
        (batch_id,),
    ).fetchall()

    layer_counts = _candidate_layer_counts(conn, batch_id)
    mainline_counts = _mainline_status_counts(conn, batch_id)
    has_trade_plan = _has_formal_trade_plan(conn, batch_id)
    reasons = _candidate_empty_reasons(conn, batch_id, run, market)

    candidates_path = output_root / "candidates_latest.csv"
    formal_count = _export_latest_candidates(conn, batch_id, candidates_path)

    markdown_path = observations_dir / f"{trade_date}.md"
    markdown = _build_observation_markdown(run, market, top_mainlines, layer_counts, mainline_counts, has_trade_plan, reasons)
    markdown_path.write_text(markdown, encoding="utf-8")

    log_path = observations_dir / "observation_log.csv"
    _append_observation_log(log_path, run, market, layer_counts, mainline_counts, has_trade_plan, reasons, markdown_path, candidates_path)

    return {
        "success": True,
        "trade_date": trade_date,
        "batch_id": batch_id,
        "market_score": round(float(market["total_score"]), 2),
        "market_status": str(market["risk_level"]),
        "top3": "；".join(
            f"{row['rank']}.{row['industry']}({float(row['base_score']):.1f})" for row in top_mainlines[:3]
        ),
        "warning_pool_count": int(layer_counts.get("预警个股池", 0)),
        "focus_pool_count": int(layer_counts.get("重点观察个股池", 0)),
        "formal_count": int(layer_counts.get("正式候选股池", formal_count)),
        "watch_count": int(layer_counts.get("重点观察个股池", 0)),
        "technical_count": int(layer_counts.get("技术突破候选", 0)),
        "near_count": int(layer_counts.get("接近候选", 0)),
        "watch_mainline_count": int(mainline_counts.get("主线预警", 0)),
        "candidate_mainline_count": int(mainline_counts.get("候选主线", 0)),
        "near_mainline_count": int(mainline_counts.get("接近确认", 0)),
        "confirmed_mainline_count": int(mainline_counts.get("确认主线", 0)),
        "downtrend_mainline_count": int(mainline_counts.get("退潮观察", 0)),
        "reasons": "；".join(reasons) if reasons else "无",
        "markdown_path": str(markdown_path),
        "candidates_path": str(candidates_path),
    }


def _candidate_layer_counts(conn, batch_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT candidate_layer, COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
        GROUP BY candidate_layer
        """,
        (batch_id,),
    ).fetchall()
    counts = {str(row["candidate_layer"]): int(row["cnt"]) for row in rows}
    for layer in ("预警个股池", "重点观察个股池", "正式候选股池", "技术突破候选", "接近候选", "剔除"):
        counts.setdefault(layer, 0)
    return counts


def _mainline_status_counts(conn, batch_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT mainline_status, COUNT(*) AS cnt
        FROM industry_score
        WHERE batch_id = ?
        GROUP BY mainline_status
        """,
        (batch_id,),
    ).fetchall()
    counts = {str(row["mainline_status"]): int(row["cnt"]) for row in rows}
    for status in ("主线预警", "候选主线", "接近确认", "确认主线", "退潮观察"):
        counts.setdefault(status, 0)
    return counts


def _has_formal_trade_plan(conn, batch_id: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
          AND suggested_action IN ('建议试错买入', '建议计划买入')
        """,
        (batch_id,),
    ).fetchone()
    return bool(row and int(row["cnt"] or 0) > 0)


def _export_latest_candidates(conn, batch_id: str, path: Path) -> int:
    rows = conn.execute(
        f"""
        SELECT {", ".join(CANDIDATE_EXPORT_COLUMNS)}
        FROM strategy_result
        WHERE batch_id = ?
          AND candidate_layer IN ('预警个股池', '重点观察个股池', '正式候选股池', '技术突破候选', '接近候选')
        ORDER BY CASE candidate_layer
                    WHEN '正式候选股池' THEN 1
                    WHEN '重点观察个股池' THEN 2
                    WHEN '预警个股池' THEN 3
                    WHEN '技术突破候选' THEN 4
                    WHEN '接近候选' THEN 5
                    ELSE 9
                 END,
                 industry_score DESC, keypoint_price DESC
        """,
        (batch_id,),
    ).fetchall()
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CANDIDATE_EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row[column] for column in CANDIDATE_EXPORT_COLUMNS})
    return len(rows)


def _candidate_empty_reasons(conn, batch_id: str, run, market) -> list[str]:
    reasons: list[str] = []
    market_score = float(market["total_score"] or 0)
    total = int(run["total_stock_count"] or 0)
    after_fund = int(run["after_fundamental_filter_count"] or 0)
    after_trend = int(run["after_trend_filter_count"] or 0)
    after_keypoint = int(run["after_keypoint_filter_count"] or 0)
    after_mainline = int(run["after_mainline_filter_count"] or 0)
    final_count = int(run["candidate_count"] or 0)

    if final_count > 0:
        return ["已有正式候选，仍需按观察期纪律复盘"]
    layer_counts = _candidate_layer_counts(conn, batch_id)
    warning_pool = int(layer_counts.get("预警个股池", 0))
    focus_pool = int(layer_counts.get("重点观察个股池", 0))
    if warning_pool > 0 or focus_pool > 0:
        reasons.append(
            f"正式候选为 0，但预警个股池 {warning_pool} 只、重点观察个股池 {focus_pool} 只，说明主线或个股正在接近条件"
        )
    if market_score < 65:
        reasons.append("市场评分低于 65，不生成正式买入建议")
    if _industry_history_days(conn) < 3:
        reasons.append("主线仍处于冷启动阶段，确认至少需要 3-5 个交易日评分历史")
    if after_keypoint == 0:
        reasons.append("关键点突破数量为 0")
    if after_mainline == 0 and after_keypoint > 0:
        reasons.append("主线未连续确认，通过技术筛选但行业未确认")
    if total > 0 and after_trend / total < 0.05:
        reasons.append("趋势模板过滤后数量过少")
    if total > 0 and after_fund / total < 0.50:
        reasons.append("基本面数据缺失或过滤较多")
    status_counts = _mainline_status_counts(conn, batch_id)
    if status_counts.get("主线预警", 0) > 0:
        reasons.append("当前存在主线预警方向，但尚未达到正式候选或确认主线标准")
    if status_counts.get("接近确认", 0) > 0:
        reasons.append("当前存在接近确认主线，说明市场方向正在形成")
    missing_finance = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
          AND rejected_reason_detail = '财务数据缺失或代理字段不足'
        """,
        (batch_id,),
    ).fetchone()
    if missing_finance and int(missing_finance["cnt"] or 0) > 0:
        reasons.append("基本面数据存在缺失或代理字段不足")
    if not reasons:
        reasons.append("当前没有股票同时满足基本面、趋势、关键点、主线和市场条件")
    return reasons


def _industry_history_days(conn) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT trade_date) AS days FROM industry_score").fetchone()
    return int(row["days"] or 0) if row else 0


def _mainline_status(row) -> str:
    status = str(row["mainline_status"] or "")
    if status:
        return status
    if int(row["confirmed"] or 0) == 1:
        return "确认主线"
    if int(row["is_candidate_mainline"] or 0) == 1:
        return "候选主线"
    return "普通"


def _suggested_position_text(score: float) -> str:
    if score >= 75:
        return "积极观察，模拟总仓位上限 60%"
    if score >= 65:
        return "可交易观察，模拟总仓位上限 40%"
    if score >= 50:
        return "轻仓观察，模拟总仓位上限 30%"
    return "防守观察，不生成买入计划"


def _build_observation_markdown(run, market, top_mainlines, layer_counts, mainline_counts, has_trade_plan: bool, reasons: list[str]) -> str:
    trade_date = str(run["trade_date"])
    market_score = float(market["total_score"])
    risk_level = str(market["risk_level"])
    conclusion = (
        f"市场评分 {market_score:.1f}，处于{risk_level}状态。"
        f"{'出现正式交易计划，仍需独立复核。' if has_trade_plan else '当前不生成正式买入计划，仅观察主线变化和候选池变化。'}"
    )
    if not has_trade_plan and (
        int(layer_counts.get("预警个股池", 0)) > 0 or int(layer_counts.get("重点观察个股池", 0)) > 0
    ):
        conclusion = (
            f"市场评分 {market_score:.1f}，处于{risk_level}状态。今日无正式候选，"
            "但存在预警/重点观察个股，说明主线正在形成或个股接近条件。建议继续观察，不生成正式买入计划。"
        )
    lines = [
        f"# 每日观察记录 - {trade_date}",
        "",
        "## 一、市场状态",
        "",
        f"- 市场评分：{market_score:.2f}",
        f"- 市场状态：{risk_level}",
        f"- 建议仓位：{_suggested_position_text(market_score)}",
        f"- 指数趋势：{float(market['index_trend_score']):.2f}",
        f"- 赚钱效应：{float(market['profit_effect_score']):.2f}",
        f"- 成交活跃：{float(market['activity_score']):.2f}",
        f"- 情绪温度：{float(market['sentiment_score']):.2f}",
        f"- 风格一致：{float(market['style_consistency_score']):.2f}",
        f"- 主线预警行业数量：{int(mainline_counts.get('主线预警', 0))}",
        f"- 候选主线行业数量：{int(mainline_counts.get('候选主线', 0))}",
        f"- 接近确认行业数量：{int(mainline_counts.get('接近确认', 0))}",
        f"- 确认主线行业数量：{int(mainline_counts.get('确认主线', 0))}",
        f"- 退潮观察行业数量：{int(mainline_counts.get('退潮观察', 0))}",
        "",
        "## 二、主线 Top5",
        "",
        "| 排名 | 行业 | base_score | total_score | mainline_status | 是否预警 | 是否接近确认 | 是否确认 | 稳定天数 |",
        "|---|---|---:|---:|---|---|---|---|---:|",
    ]
    for row in top_mainlines:
        stability = int(row["confirmed_stability_days"] or row["candidate_stability_days"] or row["stability_days"] or 0)
        lines.append(
            f"| {row['rank']} | {row['industry']} | {float(row['base_score']):.2f} | "
            f"{float(row['total_score']):.2f} | {_mainline_status(row)} | "
            f"{'是' if int(row['is_watch_mainline'] or 0) else '否'} | "
            f"{'是' if int(row['is_near_confirm'] or 0) else '否'} | "
            f"{'是' if int(row['confirmed'] or 0) else '否'} | {stability} |"
        )
    lines.extend(
        [
            "",
            "## 三、筛选漏斗",
            "",
            f"- 全市场股票数：{int(run['total_stock_count'] or 0)}",
            f"- 基础过滤后：{int(run['after_basic_filter_count'] or 0)}",
            f"- 基本面过滤后：{int(run['after_fundamental_filter_count'] or 0)}",
            f"- 趋势模板后：{int(run['after_trend_filter_count'] or 0)}",
            f"- 关键点突破后：{int(run['after_keypoint_filter_count'] or 0)}",
            f"- 主线确认后：{int(run['after_mainline_filter_count'] or 0)}",
            f"- 最终候选：{int(run['candidate_count'] or 0)}",
            "",
            "## 四、候选情况",
            "",
            f"- 预警个股池数量：{int(layer_counts.get('预警个股池', 0))}",
            f"- 重点观察个股池数量：{int(layer_counts.get('重点观察个股池', 0))}",
            f"- 正式候选股池数量：{int(layer_counts.get('正式候选股池', 0))}",
            f"- 技术突破候选数量：{int(layer_counts.get('技术突破候选', 0))}",
            f"- 接近候选数量：{int(layer_counts.get('接近候选', 0))}",
            f"- 是否出现交易计划：{'是' if has_trade_plan else '否'}",
            "",
            "## 五、候选为 0 原因",
            "",
        ]
    )
    lines.extend([f"- {reason}；" for reason in reasons])
    lines.extend(["", "## 六、今日结论", "", conclusion, ""])
    return "\n".join(lines)


def _append_observation_log(
    path: Path,
    run,
    market,
    layer_counts: dict[str, int],
    mainline_counts: dict[str, int],
    has_trade_plan: bool,
    reasons: list[str],
    markdown_path: Path,
    candidates_path: Path,
) -> None:
    columns = [
        "recorded_at",
        "trade_date",
        "batch_id",
        "market_score",
        "market_status",
        "warning_pool_count",
        "focus_pool_count",
        "formal_count",
        "watch_count",
        "technical_count",
        "near_count",
        "watch_mainline_count",
        "candidate_mainline_count",
        "near_mainline_count",
        "confirmed_mainline_count",
        "downtrend_mainline_count",
        "has_trade_plan",
        "reasons",
        "markdown_path",
        "candidates_path",
        "status",
        "error",
    ]
    write_header = not path.exists()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)
            if reader.fieldnames != columns:
                with path.open("w", newline="", encoding="utf-8-sig") as out:
                    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for row in existing_rows:
                        writer.writerow({column: row.get(column, "") for column in columns})
                write_header = False
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "trade_date": run["trade_date"],
                "batch_id": run["batch_id"],
                "market_score": round(float(market["total_score"]), 2),
                "market_status": market["risk_level"],
                "warning_pool_count": int(layer_counts.get("预警个股池", 0)),
                "focus_pool_count": int(layer_counts.get("重点观察个股池", 0)),
                "formal_count": int(layer_counts.get("正式候选股池", 0)),
                "watch_count": int(layer_counts.get("重点观察个股池", 0)),
                "technical_count": int(layer_counts.get("技术突破候选", 0)),
                "near_count": int(layer_counts.get("接近候选", 0)),
                "watch_mainline_count": int(mainline_counts.get("主线预警", 0)),
                "candidate_mainline_count": int(mainline_counts.get("候选主线", 0)),
                "near_mainline_count": int(mainline_counts.get("接近确认", 0)),
                "confirmed_mainline_count": int(mainline_counts.get("确认主线", 0)),
                "downtrend_mainline_count": int(mainline_counts.get("退潮观察", 0)),
                "has_trade_plan": int(has_trade_plan),
                "reasons": "；".join(reasons),
                "markdown_path": str(markdown_path),
                "candidates_path": str(candidates_path),
                "status": "success",
                "error": "",
            }
        )


def _format_observation_output(observation: dict[str, object]) -> str:
    return "\n".join(
        [
            f"今日是否运行成功：{observation['success']}",
            f"交易日期：{observation['trade_date']}",
            f"市场评分：{observation['market_score']}",
            f"市场状态：{observation['market_status']}",
            f"主线 Top3：{observation['top3']}",
            f"预警个股池数量：{observation['warning_pool_count']}",
            f"重点观察个股池数量：{observation['focus_pool_count']}",
            f"正式候选股池数量：{observation['formal_count']}",
            f"技术突破候选数量：{observation['technical_count']}",
            f"接近候选数量：{observation['near_count']}",
            f"主线预警行业数量：{observation['watch_mainline_count']}",
            f"候选主线行业数量：{observation['candidate_mainline_count']}",
            f"接近确认行业数量：{observation['near_mainline_count']}",
            f"确认主线行业数量：{observation['confirmed_mainline_count']}",
            f"退潮观察行业数量：{observation['downtrend_mainline_count']}",
            f"候选为 0 的主要原因：{observation['reasons']}",
            f"观察记录文件路径：{observation['markdown_path']}",
            f"candidates_latest.csv 文件路径：{observation['candidates_path']}",
        ]
    )

def _current_source(conn) -> str:
    row = conn.execute("SELECT data_source FROM data_snapshot WHERE id = 1").fetchone()
    return str(row["data_source"]) if row else ""


def _reusable_trade_dates(conn, keep_recent: int = 5) -> set[str]:
    rows = conn.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date").fetchall()
    dates = [str(row["trade_date"]) for row in rows]
    if len(dates) <= keep_recent:
        return set()
    return set(dates[:-keep_recent])


if __name__ == "__main__":
    main()

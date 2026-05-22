from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import DATA_DIR, DEFAULT_DB_PATH


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS stock_basic (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    list_date TEXT,
    is_st INTEGER NOT NULL DEFAULT 0,
    is_delist_risk INTEGER NOT NULL DEFAULT 0,
    is_suspended INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS index_daily (
    index_code TEXT NOT NULL,
    index_name TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_daily (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    pct_chg REAL NOT NULL,
    turnover_rate REAL NOT NULL,
    is_suspended INTEGER NOT NULL DEFAULT 0,
    is_limit_up INTEGER NOT NULL DEFAULT 0,
    is_limit_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS industry_daily (
    industry TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close REAL NOT NULL,
    pct_chg REAL NOT NULL,
    amount REAL NOT NULL,
    up_count INTEGER NOT NULL,
    down_count INTEGER NOT NULL,
    limit_up_count INTEGER NOT NULL,
    PRIMARY KEY (industry, trade_date)
);

CREATE TABLE IF NOT EXISTS financials (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    net_profit REAL NOT NULL,
    deducted_net_profit REAL NOT NULL,
    revenue_yoy REAL NOT NULL,
    asset_liability_ratio REAL NOT NULL,
    net_profit_missing INTEGER NOT NULL DEFAULT 0,
    deducted_net_profit_missing INTEGER NOT NULL DEFAULT 0,
    data_quality_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, report_date)
);

CREATE TABLE IF NOT EXISTS data_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_source TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    stock_count INTEGER NOT NULL,
    latest_trade_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_score (
    batch_id TEXT PRIMARY KEY,
    run_timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    index_trend_score REAL NOT NULL,
    profit_effect_score REAL NOT NULL,
    activity_score REAL NOT NULL,
    sentiment_score REAL NOT NULL,
    style_consistency_score REAL NOT NULL,
    total_score REAL NOT NULL,
    risk_level TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industry_score (
    batch_id TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    industry TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    base_score REAL NOT NULL DEFAULT 0,
    total_score REAL NOT NULL DEFAULT 0,
    momentum_score REAL NOT NULL,
    breadth_score REAL NOT NULL,
    amount_score REAL NOT NULL,
    persistence_score REAL NOT NULL DEFAULT 0,
    strength_score REAL NOT NULL DEFAULT 0,
    width_score REAL NOT NULL DEFAULT 0,
    capacity_score REAL NOT NULL DEFAULT 0,
    hot_score REAL NOT NULL DEFAULT 0,
    leader_score REAL NOT NULL DEFAULT 0,
    leader_250_high_count INTEGER NOT NULL DEFAULT 0,
    leader_60_high_count INTEGER NOT NULL DEFAULT 0,
    leader_trend_middle_count INTEGER NOT NULL DEFAULT 0,
    logic_score REAL NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL,
    is_candidate_mainline INTEGER NOT NULL,
    mainline_status TEXT NOT NULL DEFAULT '普通',
    strong_streak_days INTEGER NOT NULL DEFAULT 0,
    candidate_stability_days INTEGER NOT NULL DEFAULT 0,
    confirmed_stability_days INTEGER NOT NULL DEFAULT 0,
    rank_top5_avg REAL NOT NULL DEFAULT 0,
    amount_ratio REAL NOT NULL DEFAULT 1,
    is_watch_mainline INTEGER NOT NULL DEFAULT 0,
    is_near_confirm INTEGER NOT NULL DEFAULT 0,
    is_downtrend_watch INTEGER NOT NULL DEFAULT 0,
    stability_days INTEGER NOT NULL DEFAULT 0,
    rank_change INTEGER NOT NULL DEFAULT 0,
    score_change REAL NOT NULL DEFAULT 0,
    drift_flag INTEGER NOT NULL DEFAULT 0,
    drift_status TEXT NOT NULL,
    PRIMARY KEY (batch_id, industry)
);

CREATE TABLE IF NOT EXISTS strategy_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_layer TEXT NOT NULL DEFAULT '剔除',
    entry_channel TEXT NOT NULL DEFAULT '剔除通道',
    candidate_grade TEXT NOT NULL DEFAULT '剔除',
    mainline_status TEXT NOT NULL DEFAULT '',
    mainline_base_score REAL NOT NULL DEFAULT 0,
    keypoint_distance_pct REAL,
    signal_status TEXT NOT NULL DEFAULT '',
    market_score REAL NOT NULL,
    industry_score REAL NOT NULL,
    fundamental_status TEXT NOT NULL DEFAULT '',
    trend_template_type TEXT NOT NULL DEFAULT '',
    volume_quality TEXT NOT NULL DEFAULT '',
    close_quality TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT '',
    trade_plan_type TEXT NOT NULL DEFAULT '',
    suggested_action TEXT,
    keypoint_date TEXT,
    keypoint_price REAL,
    keypoint_type TEXT,
    breakout_date TEXT,
    breakout_close REAL,
    breakout_day_low REAL,
    breakout_ma10 REAL,
    pullback_low REAL,
    confirm_date TEXT,
    confirm_close REAL,
    pullback_volume_shrink INTEGER,
    confirm_volume_expand INTEGER,
    action TEXT,
    watch_price REAL,
    trigger_price REAL,
    buy_lower REAL,
    buy_upper REAL,
    suggested_buy_price REAL,
    stop_loss_price REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    trailing_stop_rule TEXT,
    suggested_position REAL,
    buy_range_low REAL,
    buy_range_high REAL,
    stop_loss REAL,
    take_profit REAL,
    moving_take_profit_rule TEXT,
    position_pct REAL,
    include_reason TEXT NOT NULL,
    exclude_reason TEXT NOT NULL,
    rejected_reason_detail TEXT NOT NULL DEFAULT '',
    risk_warning TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_pool (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    source_batch_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    note TEXT NOT NULL,
    trade_plan_type TEXT,
    suggested_action TEXT,
    watch_price REAL,
    trigger_price REAL,
    buy_lower REAL,
    buy_upper REAL,
    suggested_buy_price REAL,
    stop_loss_price REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    trailing_stop_rule TEXT,
    suggested_position REAL,
    risk_warning TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    batch_id TEXT PRIMARY KEY,
    run_timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    data_source TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    total_stock_count INTEGER NOT NULL DEFAULT 0,
    after_basic_filter_count INTEGER NOT NULL DEFAULT 0,
    after_fundamental_filter_count INTEGER NOT NULL DEFAULT 0,
    after_trend_filter_count INTEGER NOT NULL DEFAULT 0,
    after_keypoint_filter_count INTEGER NOT NULL DEFAULT 0,
    after_mainline_filter_count INTEGER NOT NULL DEFAULT 0,
    after_market_filter_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_code_date ON stock_daily (code, trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily (trade_date);
CREATE INDEX IF NOT EXISTS idx_industry_daily_industry_date ON industry_daily (industry, trade_date);
CREATE INDEX IF NOT EXISTS idx_financials_code_date ON financials (code, report_date);
"""


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        migrate_schema(conn)


MIGRATION_COLUMNS = {
    "stock_basic": {
        "is_suspended": "INTEGER NOT NULL DEFAULT 0",
    },
    "stock_daily": {
        "is_suspended": "INTEGER NOT NULL DEFAULT 0",
        "is_limit_up": "INTEGER NOT NULL DEFAULT 0",
        "is_limit_down": "INTEGER NOT NULL DEFAULT 0",
    },
    "financials": {
        "net_profit_missing": "INTEGER NOT NULL DEFAULT 0",
        "deducted_net_profit_missing": "INTEGER NOT NULL DEFAULT 0",
        "data_quality_note": "TEXT NOT NULL DEFAULT ''",
    },
    "industry_score": {
        "base_score": "REAL NOT NULL DEFAULT 0",
        "total_score": "REAL NOT NULL DEFAULT 0",
        "hot_score": "REAL NOT NULL DEFAULT 0",
        "persistence_score": "REAL NOT NULL DEFAULT 0",
        "strength_score": "REAL NOT NULL DEFAULT 0",
        "width_score": "REAL NOT NULL DEFAULT 0",
        "capacity_score": "REAL NOT NULL DEFAULT 0",
        "leader_score": "REAL NOT NULL DEFAULT 0",
        "leader_250_high_count": "INTEGER NOT NULL DEFAULT 0",
        "leader_60_high_count": "INTEGER NOT NULL DEFAULT 0",
        "leader_trend_middle_count": "INTEGER NOT NULL DEFAULT 0",
        "logic_score": "REAL NOT NULL DEFAULT 0",
        "mainline_status": "TEXT NOT NULL DEFAULT '普通'",
        "strong_streak_days": "INTEGER NOT NULL DEFAULT 0",
        "rank_top5_avg": "REAL NOT NULL DEFAULT 0",
        "amount_ratio": "REAL NOT NULL DEFAULT 1",
        "is_watch_mainline": "INTEGER NOT NULL DEFAULT 0",
        "is_near_confirm": "INTEGER NOT NULL DEFAULT 0",
        "is_downtrend_watch": "INTEGER NOT NULL DEFAULT 0",
        "candidate_stability_days": "INTEGER NOT NULL DEFAULT 0",
        "confirmed_stability_days": "INTEGER NOT NULL DEFAULT 0",
        "stability_days": "INTEGER NOT NULL DEFAULT 0",
        "rank_change": "INTEGER NOT NULL DEFAULT 0",
        "score_change": "REAL NOT NULL DEFAULT 0",
        "drift_flag": "INTEGER NOT NULL DEFAULT 0",
    },
    "strategy_result": {
        "candidate_layer": "TEXT NOT NULL DEFAULT '剔除'",
        "entry_channel": "TEXT NOT NULL DEFAULT '剔除通道'",
        "candidate_grade": "TEXT NOT NULL DEFAULT '剔除'",
        "mainline_status": "TEXT NOT NULL DEFAULT ''",
        "mainline_base_score": "REAL NOT NULL DEFAULT 0",
        "keypoint_distance_pct": "REAL",
        "signal_status": "TEXT NOT NULL DEFAULT ''",
        "fundamental_status": "TEXT NOT NULL DEFAULT ''",
        "trend_template_type": "TEXT NOT NULL DEFAULT ''",
        "volume_quality": "TEXT NOT NULL DEFAULT ''",
        "close_quality": "TEXT NOT NULL DEFAULT ''",
        "risk_level": "TEXT NOT NULL DEFAULT ''",
        "trade_plan_type": "TEXT NOT NULL DEFAULT ''",
        "suggested_action": "TEXT",
        "breakout_date": "TEXT",
        "breakout_close": "REAL",
        "breakout_day_low": "REAL",
        "breakout_ma10": "REAL",
        "pullback_low": "REAL",
        "confirm_date": "TEXT",
        "confirm_close": "REAL",
        "pullback_volume_shrink": "INTEGER",
        "confirm_volume_expand": "INTEGER",
        "buy_lower": "REAL",
        "buy_upper": "REAL",
        "suggested_buy_price": "REAL",
        "stop_loss_price": "REAL",
        "take_profit_1": "REAL",
        "take_profit_2": "REAL",
        "trailing_stop_rule": "TEXT",
        "suggested_position": "REAL",
        "rejected_reason_detail": "TEXT NOT NULL DEFAULT ''",
    },
    "watch_pool": {
        "trade_plan_type": "TEXT",
        "suggested_action": "TEXT",
        "watch_price": "REAL",
        "trigger_price": "REAL",
        "buy_lower": "REAL",
        "buy_upper": "REAL",
        "suggested_buy_price": "REAL",
        "stop_loss_price": "REAL",
        "take_profit_1": "REAL",
        "take_profit_2": "REAL",
        "trailing_stop_rule": "TEXT",
        "suggested_position": "REAL",
        "risk_warning": "TEXT",
    },
    "run_log": {
        "total_stock_count": "INTEGER NOT NULL DEFAULT 0",
        "after_basic_filter_count": "INTEGER NOT NULL DEFAULT 0",
        "after_fundamental_filter_count": "INTEGER NOT NULL DEFAULT 0",
        "after_trend_filter_count": "INTEGER NOT NULL DEFAULT 0",
        "after_keypoint_filter_count": "INTEGER NOT NULL DEFAULT 0",
        "after_mainline_filter_count": "INTEGER NOT NULL DEFAULT 0",
        "after_market_filter_count": "INTEGER NOT NULL DEFAULT 0",
    },
}


def migrate_schema(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATION_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


def write_frame(
    conn: sqlite3.Connection,
    table: str,
    frame: pd.DataFrame,
    replace: bool = False,
) -> None:
    if frame.empty:
        return
    mode = "replace" if replace else "append"
    frame.to_sql(table, conn, if_exists=mode, index=False)


def upsert_frame(
    conn: sqlite3.Connection,
    table: str,
    frame: pd.DataFrame,
    key_columns: Iterable[str],
) -> None:
    if frame.empty:
        return
    columns = list(frame.columns)
    placeholders = ",".join(["?"] * len(columns))
    column_sql = ",".join(columns)
    update_columns = [c for c in columns if c not in set(key_columns)]
    update_sql = ",".join([f"{c}=excluded.{c}" for c in update_columns])
    sql = (
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT({','.join(key_columns)}) DO UPDATE SET {update_sql}"
    )
    conn.executemany(sql, frame[columns].itertuples(index=False, name=None))
    conn.commit()


def bulk_insert_frame(conn: sqlite3.Connection, table: str, frame: pd.DataFrame, chunksize: int = 50000) -> None:
    if frame.empty:
        return
    frame.to_sql(table, conn, if_exists="append", index=False, chunksize=chunksize)
    conn.commit()


def read_sql(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])

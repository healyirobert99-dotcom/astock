from __future__ import annotations

import os
from pathlib import Path

import pytest

from a_stock_selector.config import StrategyConfig
from a_stock_selector.data_provider import TushareDataProvider, save_dataset
from a_stock_selector.db import connect, init_db, table_count
from a_stock_selector.strategy import run_strategy


@pytest.mark.slow
@pytest.mark.realdata
def test_tushare_real_data_smoke(tmp_path: Path) -> None:
    if not os.getenv("TUSHARE_TOKEN"):
        pytest.skip("TUSHARE_TOKEN is not configured")

    db_path = tmp_path / "realdata_smoke.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        dataset = TushareDataProvider(max_stocks=50, lookback_days=300).fetch()
        assert not dataset.stock_basic.empty
        assert not dataset.stock_daily.empty
        assert not dataset.index_daily.empty

        save_dataset(conn, dataset)
        assert table_count(conn, "stock_basic") > 0
        assert table_count(conn, "stock_daily") > 0

        summary = run_strategy(conn, StrategyConfig())
        assert summary.batch_id
        market = conn.execute(
            "SELECT total_score FROM market_score WHERE batch_id = ?",
            (summary.batch_id,),
        ).fetchone()
        assert market is not None
        assert 0 <= float(market["total_score"]) <= 100

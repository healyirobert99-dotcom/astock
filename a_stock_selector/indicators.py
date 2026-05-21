from __future__ import annotations

import pandas as pd


def moving_average(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def pct_rank(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        return 0.0
    return max(0.0, min(100.0, (value - lower) / (upper - lower) * 100.0))


def latest_trade_date(frames: list[pd.DataFrame]) -> str:
    dates = []
    for frame in frames:
        if "trade_date" in frame.columns and not frame.empty:
            dates.append(str(frame["trade_date"].max()))
    if not dates:
        raise RuntimeError("No trade_date found in loaded market data")
    return max(dates)


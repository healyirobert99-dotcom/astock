from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "a_stock_selector.sqlite3"

RISK_WARNING = (
    "本系统交易建议仅基于规则模型生成，不保证盈利或本金安全。"
    "请严格控制仓位，独立承担风险。"
)


@dataclass(frozen=True)
class StrategyConfig:
    MARKET_MIN_SCORE: float = 50.0
    MARKET_TRADE_SCORE: float = 65.0

    MAINLINE_CANDIDATE_SCORE: float = 70.0
    MAINLINE_WATCH_SCORE: float = 65.0
    MAINLINE_NEAR_CONFIRM_SCORE: float = 75.0
    MAINLINE_CONFIRMED_SCORE: float = 80.0
    MAINLINE_CONFIRM_DAYS: int = 3
    MAINLINE_CANDIDATE_DAYS: int = 2
    MAINLINE_CONFIRMED_DAYS: int = 3
    MAINLINE_WATCH_RANK_PCT: float = 0.10
    MAINLINE_WATCH_AMOUNT_RATIO: float = 1.20
    MAINLINE_DOWNGRADE_SCORE: float = 65.0
    MAINLINE_DOWNGRADE_DAYS: int = 2
    MAINLINE_REMOVE_SCORE: float = 60.0
    MAINLINE_REMOVE_DAYS: int = 3

    MIN_AVG_AMOUNT_20D: float = 100_000_000.0
    MIN_LIST_DAYS: int = 250
    MIN_PRICE: float = 3.0
    MIN_TURNOVER_20D: float = 1.0

    MAX_DEBT_RATIO: float = 75.0
    MIN_REVENUE_YOY: float = -10.0

    VOLUME_BREAKOUT_MIN_RATIO_5D: float = 1.5
    VOLUME_BREAKOUT_MIN_RATIO_20D: float = 1.3
    VOLUME_BREAKOUT_MAX_RATIO_20D: float = 2.5

    CLOSE_HIGH_MIN_RATIO: float = 0.98
    CLOSE_MA20_MAX_RATIO: float = 1.20
    LEADER_CLOSE_MA20_MAX_RATIO: float = 1.15

    STOP_LOSS_FALLBACK: float = 0.08
    MIN_HISTORY_DAYS: int = 260

    @property
    def mainline_confirm_days(self) -> int:
        return self.MAINLINE_CONFIRM_DAYS

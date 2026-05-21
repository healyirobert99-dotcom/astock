from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT
from .db import bulk_insert_frame, read_sql, upsert_frame

ProgressCallback = Callable[[str, int, int], None]


@dataclass
class MarketDataset:
    stock_basic: pd.DataFrame
    index_daily: pd.DataFrame
    stock_daily: pd.DataFrame
    industry_daily: pd.DataFrame
    financials: pd.DataFrame
    source_name: str


class DataProvider(Protocol):
    source_name: str

    def fetch(self) -> MarketDataset:
        ...


class SampleDataProvider:
    source_name = "sample"

    def __init__(self, end_date: date | None = None) -> None:
        self.end_date = pd.Timestamp(end_date or date.today()).normalize()

    def fetch(self) -> MarketDataset:
        trade_dates = pd.bdate_range(end=self.end_date, periods=330)
        stocks = pd.DataFrame(
            [
                ("000001", "平安银行", "银行", "19910403", 0, 0),
                ("000063", "中兴通讯", "通信设备", "19971118", 0, 0),
                ("000333", "美的集团", "家电", "20130918", 0, 0),
                ("000651", "格力电器", "家电", "19961118", 0, 0),
                ("002230", "科大讯飞", "软件服务", "20080512", 0, 0),
                ("002415", "海康威视", "电子设备", "20100528", 0, 0),
                ("300059", "东方财富", "互联网金融", "20100319", 0, 0),
                ("300750", "宁德时代", "电池", "20180611", 0, 0),
                ("600519", "贵州茅台", "白酒", "20010827", 0, 0),
                ("600600", "青岛啤酒", "食品饮料", "19930827", 0, 0),
                ("600000", "浦发银行", "银行", "19991110", 0, 0),
                ("000999", "ST 样例", "医药", "20000309", 1, 0),
                ("600999", "退市风险样例", "证券", "20091030", 0, 1),
            ],
            columns=["code", "name", "industry", "list_date", "is_st", "is_delist_risk"],
        )
        stocks["is_suspended"] = 0
        index_daily = self._build_index_daily(trade_dates)
        stock_daily = self._build_stock_daily(stocks, trade_dates)
        industry_daily = self._build_industry_daily(stocks, stock_daily, trade_dates)
        financials = self._build_financials(stocks)
        return MarketDataset(stocks, index_daily, stock_daily, industry_daily, financials, self.source_name)

    def _build_index_daily(self, trade_dates: pd.DatetimeIndex) -> pd.DataFrame:
        rows: list[dict] = []
        specs = [
            ("000300", "沪深300", 4100, 0.00045),
            ("000905", "中证500", 5600, 0.00050),
            ("000852", "中证1000", 5900, 0.00055),
            ("399303", "国证2000", 8200, 0.00060),
            ("399006", "创业板指", 1900, 0.00085),
            ("000001", "上证指数", 3050, 0.00045),
            ("399001", "深证成指", 9600, 0.00065),
        ]
        for idx, (code, name, start, drift) in enumerate(specs):
            prices = self._price_series(len(trade_dates), start, drift, 0.012, idx + 10)
            for i, trade_date in enumerate(trade_dates):
                close = prices[i]
                prev = prices[i - 1] if i else close * 0.995
                open_price = prev * (1 + math.sin(i / 17 + idx) * 0.002)
                high = max(open_price, close) * 1.006
                low = min(open_price, close) * 0.994
                amount = (3800 + idx * 900 + i * 4) * 100_000_000
                rows.append(
                    {
                        "index_code": code,
                        "index_name": name,
                        "trade_date": trade_date.strftime("%Y-%m-%d"),
                        "open": round(open_price, 2),
                        "high": round(high, 2),
                        "low": round(low, 2),
                        "close": round(close, 2),
                        "volume": round(amount / close, 2),
                        "amount": round(amount, 2),
                    }
                )
        return pd.DataFrame(rows)

    def _build_stock_daily(self, stocks: pd.DataFrame, trade_dates: pd.DatetimeIndex) -> pd.DataFrame:
        rows: list[dict] = []
        industry_bias = {
            "软件服务": 0.0016,
            "通信设备": 0.0013,
            "电子设备": 0.0012,
            "互联网金融": 0.001,
            "电池": 0.0008,
            "家电": 0.0002,
            "白酒": 0.0001,
            "食品饮料": 0.0001,
            "银行": -0.0001,
            "医药": -0.0003,
            "证券": 0.0001,
        }
        for stock_idx, stock in stocks.reset_index(drop=True).iterrows():
            start = 8 + stock_idx * 6
            drift = industry_bias.get(stock["industry"], 0.0003)
            prices = self._price_series(len(trade_dates), start, drift, 0.02, stock_idx)
            if stock["code"] in {"002230", "000063", "002415"}:
                prices[-130:] *= np.linspace(0.92, 1.45, 130)
                prices[-1] = max(prices[-130:-1]) * 1.025
            if stock["code"] == "002230":
                # Keep one deterministic full-pass sample: strong smooth trend,
                # confirmed industry strength, and a final valid breakout.
                tail_start = max(prices[-61], prices[-120:-60].mean())
                prices[-60:-1] = np.linspace(tail_start, tail_start * 1.85, 59)
                prices[-1] = prices[-2] * 1.035
            if stock["code"] == "300059":
                prices[-120:] *= np.linspace(0.95, 1.25, 120)
                prices[-1] = max(prices[-120:-1]) * 1.012
            if stock["is_st"] or stock["is_delist_risk"]:
                prices[-90:] *= np.linspace(1.0, 0.72, 90)
            for i, trade_date in enumerate(trade_dates):
                close = max(float(prices[i]), 1.0)
                prev = max(float(prices[i - 1]), 1.0) if i else close * 0.99
                intraday_wave = math.sin(i / 8 + stock_idx) * 0.006
                open_price = prev * (1 + intraday_wave)
                high = max(open_price, close) * (1.01 + abs(math.sin(i / 11 + stock_idx)) * 0.01)
                low = min(open_price, close) * (0.99 - abs(math.cos(i / 13 + stock_idx)) * 0.004)
                pct_chg = (close / prev - 1) * 100
                volume_base = 18_000_000 + stock_idx * 2_500_000
                volume = volume_base * (1 + abs(math.sin(i / 18 + stock_idx)) * 0.45)
                if stock["code"] == "002230" and i == len(trade_dates) - 1:
                    volume *= 1.7
                turnover_rate = min(18.0, 1.8 + stock_idx * 0.25 + abs(pct_chg) * 0.4)
                rows.append(
                    {
                        "code": stock["code"],
                        "trade_date": trade_date.strftime("%Y-%m-%d"),
                        "open": round(open_price, 2),
                        "high": round(high, 2),
                        "low": round(low, 2),
                        "close": round(close, 2),
                        "volume": round(volume, 2),
                        "amount": round(volume * close, 2),
                        "pct_chg": round(pct_chg, 2),
                        "turnover_rate": round(turnover_rate, 2),
                        "is_suspended": 0,
                        "is_limit_up": int(pct_chg >= 9.5),
                        "is_limit_down": int(pct_chg <= -9.5),
                    }
                )
        return pd.DataFrame(rows)

    def _build_industry_daily(
        self,
        stocks: pd.DataFrame,
        stock_daily: pd.DataFrame,
        trade_dates: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        merged = stock_daily.merge(stocks[["code", "industry"]], on="code", how="left")
        rows: list[dict] = []
        for industry, group in merged.groupby("industry"):
            day_group = group.groupby("trade_date")
            base = 1000 + len(industry) * 30
            industry_close = base
            prev_close = industry_close
            for trade_date in trade_dates.strftime("%Y-%m-%d"):
                day = day_group.get_group(trade_date)
                avg_pct = float(day["pct_chg"].mean())
                industry_close = max(200, prev_close * (1 + avg_pct / 100))
                rows.append(
                    {
                        "industry": industry,
                        "trade_date": trade_date,
                        "close": round(industry_close, 2),
                        "pct_chg": round(avg_pct, 2),
                        "amount": round(float(day["amount"].sum()), 2),
                        "up_count": int((day["pct_chg"] > 0).sum()),
                        "down_count": int((day["pct_chg"] < 0).sum()),
                        "limit_up_count": int((day["pct_chg"] >= 9.5).sum()),
                    }
                )
                prev_close = industry_close
        return pd.DataFrame(rows)

    def _build_financials(self, stocks: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for i, stock in stocks.reset_index(drop=True).iterrows():
            bad = stock["is_st"] or stock["is_delist_risk"]
            rows.append(
                {
                    "code": stock["code"],
                    "report_date": f"{self.end_date.year - 1}-12-31",
                    "net_profit": -3.2e8 if bad else 2.5e8 + i * 1.1e8,
                    "deducted_net_profit": -2.8e8 if bad else 2.0e8 + i * 0.85e8,
                    "revenue_yoy": -12.0 if bad else 4.0 + i * 1.7,
                    "asset_liability_ratio": 82.0 if bad else min(68.0, 35.0 + i * 2.2),
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _price_series(length: int, start: float, drift: float, vol: float, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        shocks = rng.normal(drift, vol, length)
        cycle = np.sin(np.arange(length) / 23 + seed) * vol * 0.35
        returns = shocks + cycle
        prices = start * np.cumprod(1 + returns)
        return np.maximum(prices, 1.0)


class AKShareDataProvider:
    source_name = "akshare"

    def __init__(
        self,
        max_stocks: int | None = None,
        lookback_days: int | None = None,
        existing_stock_basic: pd.DataFrame | None = None,
        existing_financials: pd.DataFrame | None = None,
        existing_latest_trade_date: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.max_stocks = max_stocks if max_stocks is not None else _load_int_env("FREE_MAX_STOCKS", 0)
        self.lookback_days = lookback_days if lookback_days is not None else _load_int_env("FREE_LOOKBACK_DAYS", 430)
        self.existing_stock_basic = existing_stock_basic
        self.existing_financials = existing_financials
        self.existing_latest_trade_date = existing_latest_trade_date
        self.progress_callback = progress_callback

    def _emit(self, message: str, current: int, total: int = 100) -> None:
        if self.progress_callback:
            self.progress_callback(message, current, total)

    def fetch(self) -> MarketDataset:
        self._emit("连接免费数据源 AKShare / 腾讯 / 东方财富", 0, 100)
        stock_basic = self._stock_basic()
        if self.max_stocks and self.max_stocks > 0:
            stock_basic = stock_basic.head(self.max_stocks).copy()
        if stock_basic.empty:
            raise RuntimeError("免费数据源未能获得股票池")

        start_date, end_date = self._date_range()
        index_daily = self._fetch_index_daily(start_date, end_date)
        latest_index_date = str(index_daily["trade_date"].max()) if not index_daily.empty else ""
        stock_daily = self._fetch_stock_daily(stock_basic, start_date, end_date, latest_index_date)
        if stock_daily.empty:
            raise RuntimeError("免费数据源未能获得个股日线")
        latest_trade_date = str(stock_daily["trade_date"].max())
        stock_basic, stock_daily = _mark_suspended_by_latest_bar(stock_basic, stock_daily, latest_trade_date)
        industry_daily = _build_industry_daily_from_frames(stock_basic, stock_daily)
        financials = self._financials(stock_basic)
        return MarketDataset(stock_basic, index_daily, stock_daily, industry_daily, financials, self.source_name)

    def _stock_basic(self) -> pd.DataFrame:
        existing = self.existing_stock_basic.copy() if self.existing_stock_basic is not None else pd.DataFrame()
        if not existing.empty:
            self._emit(f"复用本地股票池：{len(existing)} 只", 5, 100)
            required = ["code", "name", "industry", "list_date", "is_st", "is_delist_risk", "is_suspended"]
            for column in required:
                if column not in existing.columns:
                    existing[column] = "" if column in {"name", "industry", "list_date"} else 0
            existing["code"] = existing["code"].astype(str).str.zfill(6)
            existing["industry"] = existing["industry"].fillna("未分类").replace("", "未分类")
            return existing[required].drop_duplicates("code", keep="last")

        self._emit("读取免费股票列表", 5, 100)
        import akshare as ak

        spot = _akshare_call_with_retries(ak.stock_zh_a_spot_em)
        if spot.empty or not {"代码", "名称"}.issubset(set(spot.columns)):
            spot = _akshare_call_with_retries(ak.stock_zh_a_spot)
        if spot.empty or not {"代码", "名称"}.issubset(set(spot.columns)):
            raise RuntimeError("免费股票列表接口返回字段不完整")
        if "所属行业" not in spot.columns:
            spot["所属行业"] = "未分类"
        basics = pd.DataFrame(
            {
                "code": spot["代码"].astype(str),
                "name": spot["名称"].astype(str),
                "industry": spot["所属行业"].astype(str).replace("", "未分类"),
                "list_date": "",
                "is_st": spot["名称"].astype(str).str.contains("ST").astype(int),
                "is_delist_risk": spot["名称"].astype(str).str.contains("退").astype(int),
            }
        )
        basics["is_suspended"] = 0
        basics["code"] = basics["code"].astype(str).str.zfill(6)
        return basics.drop_duplicates("code", keep="last")

    def _date_range(self) -> tuple[str, str]:
        end = date.today()
        if self.existing_latest_trade_date:
            try:
                start = pd.Timestamp(self.existing_latest_trade_date).date() - timedelta(days=10)
            except Exception:
                start = end - timedelta(days=self.lookback_days)
        else:
            start = end - timedelta(days=self.lookback_days)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    def _fetch_stock_daily(
        self,
        stock_basic: pd.DataFrame,
        start_date: str,
        end_date: str,
        latest_trade_date: str = "",
    ) -> pd.DataFrame:
        import akshare as ak

        spot_daily = self._fetch_spot_daily(stock_basic, latest_trade_date)
        if len(spot_daily) >= max(1, int(len(stock_basic) * 0.80)):
            return spot_daily

        rows = []
        total = len(stock_basic)
        for idx, code in enumerate(stock_basic["code"].astype(str), start=1):
            if idx == 1 or idx % 100 == 0 or idx == total:
                self._emit(f"免费源读取个股日线：{idx}/{total}", 10 + int(idx / max(total, 1) * 55), 100)
            hist = pd.DataFrame()
            try:
                hist = _akshare_call_with_retries(
                    ak.stock_zh_a_hist_tx,
                    symbol=_tencent_stock_symbol(code),
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                    retries=2,
                    delay_seconds=0.5,
                )
                hist = _normalize_tencent_stock_hist(hist, code)
            except Exception:
                hist = pd.DataFrame()
            if hist.empty:
                try:
                    hist = _akshare_call_with_retries(
                        ak.stock_zh_a_hist,
                        symbol=code,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust="qfq",
                        retries=2,
                        delay_seconds=0.5,
                    )
                except Exception:
                    hist = pd.DataFrame()
                if not hist.empty:
                    hist = _normalize_eastmoney_stock_hist(hist)
            if not hist.empty:
                if latest_trade_date:
                    hist = hist[hist["trade_date"].astype(str) >= latest_trade_date].copy()
            if not hist.empty:
                rows.append(hist)
        if not rows:
            return _empty_stock_daily_frame()
        daily = pd.concat(rows, ignore_index=True)
        daily["is_suspended"] = 0
        daily["is_limit_up"] = daily["pct_chg"].ge(9.5).astype(int)
        daily["is_limit_down"] = daily["pct_chg"].le(-9.5).astype(int)
        return daily

    def _fetch_spot_daily(self, stock_basic: pd.DataFrame, latest_trade_date: str) -> pd.DataFrame:
        import akshare as ak

        self._emit("免费源读取新浪全市场行情", 10, 100)
        try:
            spot = _akshare_call_with_retries(ak.stock_zh_a_spot, retries=2, delay_seconds=1.0)
        except Exception:
            spot = pd.DataFrame()
        if spot.empty:
            try:
                self._emit("新浪行情不可用，切换东方财富全市场行情", 10, 100)
                spot = _akshare_call_with_retries(ak.stock_zh_a_spot_em, retries=2, delay_seconds=1.0)
            except Exception:
                spot = pd.DataFrame()
        if spot.empty or not {"代码", "最新价", "今开", "最高", "最低", "成交量", "成交额", "涨跌幅"}.issubset(set(spot.columns)):
            return _empty_stock_daily_frame()
        frame = spot.copy()
        frame["code"] = frame["代码"].astype(str).str.replace(r"^[a-zA-Z]+", "", regex=True).str.zfill(6)
        allowed = set(stock_basic["code"].astype(str).str.zfill(6))
        frame = frame[frame["code"].isin(allowed)].copy()
        if frame.empty:
            return _empty_stock_daily_frame()
        trade_date = latest_trade_date or date.today().strftime("%Y-%m-%d")
        daily = pd.DataFrame(
            {
                "code": frame["code"],
                "trade_date": trade_date,
                "open": pd.to_numeric(frame["今开"], errors="coerce").fillna(0.0),
                "high": pd.to_numeric(frame["最高"], errors="coerce").fillna(0.0),
                "low": pd.to_numeric(frame["最低"], errors="coerce").fillna(0.0),
                "close": pd.to_numeric(frame["最新价"], errors="coerce").fillna(0.0),
                "volume": pd.to_numeric(frame["成交量"], errors="coerce").fillna(0.0) / 100,
                "amount": pd.to_numeric(frame["成交额"], errors="coerce").fillna(0.0),
                "pct_chg": pd.to_numeric(frame["涨跌幅"], errors="coerce").fillna(0.0),
                "turnover_rate": 0.0,
            }
        )
        daily = daily[(daily["close"] > 0) & (daily["high"] > 0) & (daily["low"] > 0)].copy()
        daily["is_suspended"] = 0
        daily["is_limit_up"] = daily["pct_chg"].ge(9.5).astype(int)
        daily["is_limit_down"] = daily["pct_chg"].le(-9.5).astype(int)
        self._emit(f"新浪全市场行情完成：{len(daily)} 条", 64, 100)
        return daily

    def _fetch_index_daily(self, start_date: str, end_date: str) -> pd.DataFrame:
        import akshare as ak

        specs = [
            ("sh000300", "000300", "沪深300"),
            ("sh000905", "000905", "中证500"),
            ("sh000852", "000852", "中证1000"),
            ("sz399303", "399303", "国证2000"),
            ("sz399006", "399006", "创业板指"),
            ("sh000001", "000001", "上证指数"),
            ("sz399001", "399001", "深证成指"),
        ]
        frames = []
        for idx, (symbol, index_code, index_name) in enumerate(specs, start=1):
            self._emit(f"免费源读取指数：{index_name}", 68 + int(idx / len(specs) * 10), 100)
            try:
                hist = _akshare_call_with_retries(ak.stock_zh_index_daily_tx, symbol=symbol, retries=2, delay_seconds=0.5)
            except Exception:
                hist = pd.DataFrame()
            if hist.empty:
                continue
            frame = _normalize_tencent_index_hist(hist, index_code, index_name, start_date, end_date)
            if not frame.empty:
                frames.append(frame)
        if frames:
            return pd.concat(frames, ignore_index=True)
        sample = SampleDataProvider().fetch()
        return sample.index_daily

    def _financials(self, stock_basic: pd.DataFrame) -> pd.DataFrame:
        existing = self.existing_financials.copy() if self.existing_financials is not None else pd.DataFrame()
        if not existing.empty:
            self._emit("复用本地最近财务指标", 82, 100)
            return existing
        self._emit("免费源未配置批量财务接口，使用缺失标记", 82, 100)
        return pd.DataFrame([_empty_financial_row(code) for code in stock_basic["code"].astype(str)])


class TushareDataProvider:
    source_name = "tushare"

    def __init__(
        self,
        token: str | None = None,
        max_stocks: int | None = None,
        lookback_days: int | None = None,
        skip_trade_dates: set[str] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.token = token or _load_tushare_token()
        self.max_stocks = max_stocks if max_stocks is not None else _load_int_env("TUSHARE_MAX_STOCKS", 30)
        self.lookback_days = lookback_days if lookback_days is not None else _load_int_env("TUSHARE_LOOKBACK_DAYS", 430)
        self.fetch_turnover = _load_bool_env("TUSHARE_FETCH_TURNOVER", self.max_stocks > 0)
        self.skip_trade_dates = skip_trade_dates or set()
        self.progress_callback = progress_callback
        if not self.token:
            raise RuntimeError("TUSHARE_TOKEN is not configured")

    def fetch(self) -> MarketDataset:
        import tushare as ts

        self._emit("连接 Tushare", 0, 100)
        pro = ts.pro_api(self.token)
        end = date.today()
        start = end - timedelta(days=self.lookback_days)
        start_date = start.strftime("%Y%m%d")
        end_date = end.strftime("%Y%m%d")

        self._emit("读取上市股票列表", 5, 100)
        stock_basic = self._fetch_stock_basic(pro)
        self._emit(f"股票池准备完成：{len(stock_basic)} 只", 10, 100)
        stock_daily = self._fetch_stock_daily(pro, stock_basic, start_date, end_date)
        stock_basic, stock_daily = self._apply_suspension_status(pro, stock_basic, stock_daily)
        self._emit(f"日线行情完成：{len(stock_daily)} 条", 72, 100)
        index_daily = self._fetch_index_daily(pro, start_date, end_date)
        self._emit("指数行情完成", 78, 100)
        industry_daily = self._build_industry_daily(stock_basic, stock_daily) if not stock_daily.empty else _empty_industry_daily_frame()
        self._emit("行业日线聚合完成", 84, 100)
        financials = self._fetch_financials(pro, stock_basic)
        self._emit("基础财务数据完成", 100, 100)
        return MarketDataset(
            stock_basic=stock_basic,
            index_daily=index_daily,
            stock_daily=stock_daily,
            industry_daily=industry_daily,
            financials=financials,
            source_name=self.source_name,
        )

    def _emit(self, message: str, current: int, total: int) -> None:
        if self.progress_callback:
            self.progress_callback(message, current, total)

    def _fetch_stock_basic(self, pro) -> pd.DataFrame:
        raw = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,list_date",
        )
        if raw.empty:
            raise RuntimeError("Tushare stock_basic returned no rows")
        if self.max_stocks > 0:
            raw = raw.head(self.max_stocks).copy()
        else:
            raw = raw.copy()
        return pd.DataFrame(
            {
                "code": raw["symbol"].astype(str),
                "name": raw["name"].astype(str),
                "industry": raw["industry"].fillna("未分类").astype(str),
                "list_date": raw["list_date"].fillna("").astype(str),
                "is_st": raw["name"].astype(str).str.contains("ST", case=False, na=False).astype(int),
                "is_delist_risk": raw["name"].astype(str).str.contains("退", na=False).astype(int),
                "is_suspended": 0,
                "ts_code": raw["ts_code"].astype(str),
            }
        )

    def _fetch_stock_daily(
        self,
        pro,
        stock_basic: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        if self.max_stocks <= 0:
            return self._fetch_stock_daily_by_trade_date(pro, stock_basic, start_date, end_date)
        frames = []
        total = len(stock_basic)
        for idx, (_, stock) in enumerate(stock_basic.iterrows(), start=1):
            if idx == 1 or idx % 10 == 0 or idx == total:
                self._emit(f"逐股读取日线：{idx}/{total}", 10 + int(idx / max(total, 1) * 55), 100)
            try:
                bars = pro.daily(ts_code=stock["ts_code"], start_date=start_date, end_date=end_date)
            except Exception:
                continue
            if bars.empty:
                continue
            bars = bars.copy()
            bars["code"] = stock["code"]
            turnover = self._fetch_turnover_rate(pro, stock["ts_code"], start_date, end_date)
            if not turnover.empty:
                bars = bars.merge(turnover, on=["ts_code", "trade_date"], how="left")
            else:
                bars["turnover_rate"] = 0.0
            frames.append(bars)
        if not frames:
            raise RuntimeError("Tushare daily returned no stock bars")
        raw = pd.concat(frames, ignore_index=True)
        raw["turnover_rate"] = raw["turnover_rate"].fillna(0.0)
        raw["volume"] = raw["vol"].fillna(0.0) * 100
        raw["amount_value"] = raw["amount"].fillna(0.0) * 1000
        return pd.DataFrame(
            {
                "code": raw["code"].astype(str),
                "trade_date": raw["trade_date"].map(_format_tushare_date),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),
                "amount": raw["amount_value"].astype(float),
                "pct_chg": raw["pct_chg"].fillna(0.0).astype(float),
                "turnover_rate": raw["turnover_rate"].astype(float),
                "is_suspended": 0,
                "is_limit_up": raw["pct_chg"].fillna(0.0).ge(9.5).astype(int),
                "is_limit_down": raw["pct_chg"].fillna(0.0).le(-9.5).astype(int),
            }
        ).sort_values(["code", "trade_date"])

    def _fetch_stock_daily_by_trade_date(
        self,
        pro,
        stock_basic: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        trade_dates = self._fetch_trade_dates(pro, start_date, end_date)
        if self.skip_trade_dates:
            trade_dates = [d for d in trade_dates if _format_tushare_date(d) not in self.skip_trade_dates]
        if not trade_dates:
            self._emit("本地缓存已包含全部交易日，无需拉取日线", 70, 100)
            return _empty_stock_daily_frame()
        basic_codes = stock_basic[["ts_code", "code"]].copy()
        frames = []
        total = len(trade_dates)
        for idx, trade_date in enumerate(trade_dates, start=1):
            if idx == 1 or idx % 5 == 0 or idx == total:
                self._emit(f"按交易日读取全市场日线：{idx}/{total} ({_format_tushare_date(trade_date)})", 10 + int(idx / max(total, 1) * 60), 100)
            try:
                bars = _tushare_call_with_retries(pro.daily, trade_date=trade_date)
            except Exception:
                continue
            if bars.empty:
                continue
            bars = bars.merge(basic_codes, on="ts_code", how="inner")
            if bars.empty:
                continue
            turnover = self._fetch_turnover_rate_by_trade_date(pro, trade_date) if self.fetch_turnover else pd.DataFrame()
            if not turnover.empty:
                bars = bars.merge(turnover, on=["ts_code", "trade_date"], how="left")
            else:
                bars["turnover_rate"] = 0.0
            frames.append(bars)
        if not frames:
            raise RuntimeError("Tushare daily returned no full-market stock bars")
        raw = pd.concat(frames, ignore_index=True)
        raw["turnover_rate"] = raw["turnover_rate"].fillna(0.0)
        raw["volume"] = raw["vol"].fillna(0.0) * 100
        raw["amount_value"] = raw["amount"].fillna(0.0) * 1000
        return pd.DataFrame(
            {
                "code": raw["code"].astype(str),
                "trade_date": raw["trade_date"].map(_format_tushare_date),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),
                "amount": raw["amount_value"].astype(float),
                "pct_chg": raw["pct_chg"].fillna(0.0).astype(float),
                "turnover_rate": raw["turnover_rate"].astype(float),
                "is_suspended": 0,
                "is_limit_up": raw["pct_chg"].fillna(0.0).ge(9.5).astype(int),
                "is_limit_down": raw["pct_chg"].fillna(0.0).le(-9.5).astype(int),
            }
        ).sort_values(["code", "trade_date"])

    def _fetch_trade_dates(self, pro, start_date: str, end_date: str) -> list[str]:
        try:
            calendar = _tushare_call_with_retries(
                pro.trade_cal,
                exchange="SSE",
                start_date=start_date,
                end_date=end_date,
                is_open="1",
                fields="cal_date",
            )
        except Exception:
            calendar = pd.DataFrame()
        if not calendar.empty and "cal_date" in calendar.columns:
            return sorted(calendar["cal_date"].astype(str).unique().tolist())
        return pd.bdate_range(start=start_date, end=end_date).strftime("%Y%m%d").tolist()

    def _apply_suspension_status(
        self,
        pro,
        stock_basic: pd.DataFrame,
        stock_daily: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if stock_basic.empty or stock_daily.empty:
            return stock_basic, stock_daily
        latest_trade_date = str(stock_daily["trade_date"].max())
        tushare_trade_date = latest_trade_date.replace("-", "")
        try:
            suspended = _tushare_call_with_retries(
                pro.suspend_d,
                suspend_type="S",
                trade_date=tushare_trade_date,
                fields="ts_code,trade_date,suspend_type",
            )
            if suspended.empty or "ts_code" not in suspended.columns:
                suspended_codes: set[str] = set()
            else:
                code_map = stock_basic.set_index("ts_code")["code"].to_dict()
                suspended_codes = {
                    str(code_map[ts_code])
                    for ts_code in suspended["ts_code"].astype(str)
                    if ts_code in code_map
                }
            self._emit(f"停牌接口完成：{len(suspended_codes)} 只", 70, 100)
            print(f"[INFO] Tushare suspend_d succeeded: trade_date={tushare_trade_date}, suspended={len(suspended_codes)}")
            return _mark_suspended_codes(stock_basic, stock_daily, suspended_codes, latest_trade_date)
        except Exception as exc:
            self._emit("停牌接口失败，使用最近交易日推断", 70, 100)
            print(f"[WARN] Tushare suspend_d failed, fallback to latest-bar inference: {exc}")
            return _infer_suspended_from_latest_bar(stock_basic, stock_daily, latest_trade_date)

    def _fetch_turnover_rate(self, pro, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        try:
            data = pro.daily_basic(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,turnover_rate",
            )
        except Exception:
            return pd.DataFrame()
        if data.empty or "turnover_rate" not in data.columns:
            return pd.DataFrame()
        return data[["ts_code", "trade_date", "turnover_rate"]]

    def _fetch_turnover_rate_by_trade_date(self, pro, trade_date: str) -> pd.DataFrame:
        try:
            data = _tushare_call_with_retries(
                pro.daily_basic,
                trade_date=trade_date,
                fields="ts_code,trade_date,turnover_rate",
            )
        except Exception:
            return pd.DataFrame()
        if data.empty or "turnover_rate" not in data.columns:
            return pd.DataFrame()
        return data[["ts_code", "trade_date", "turnover_rate"]]

    def _fetch_index_daily(self, pro, start_date: str, end_date: str) -> pd.DataFrame:
        specs = [
            ("000300.SH", "000300", "沪深300"),
            ("000905.SH", "000905", "中证500"),
            ("000852.SH", "000852", "中证1000"),
            ("399303.SZ", "399303", "国证2000"),
            ("000001.SH", "000001", "上证指数"),
            ("399001.SZ", "399001", "深证成指"),
            ("399006.SZ", "399006", "创业板指"),
        ]
        frames = []
        missing_indices: list[str] = []
        for ts_code, index_code, index_name in specs:
            bars = self._fetch_index_with_retry(pro, ts_code, index_name, start_date, end_date)
            if bars.empty:
                missing_indices.append(f"{index_name}({ts_code})")
                continue
            bars = bars.copy()
            bars["index_code"] = index_code
            bars["index_name"] = index_name
            frames.append(bars)
            time.sleep(0.35)

        if missing_indices:
            self._emit(f"⚠ 指数缺失: {', '.join(missing_indices)}", 0, 100)
        if not frames:
            raise RuntimeError("Tushare index_daily returned no bars for any index")
        raw = pd.concat(frames, ignore_index=True)
        raw["volume"] = raw["vol"].fillna(0.0) * 100
        raw["amount_value"] = raw["amount"].fillna(0.0) * 1000
        return pd.DataFrame(
            {
                "index_code": raw["index_code"],
                "index_name": raw["index_name"],
                "trade_date": raw["trade_date"].map(_format_tushare_date),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),
                "amount": raw["amount_value"].astype(float),
            }
        )

    @staticmethod
    def _fetch_index_with_retry(pro, ts_code: str, index_name: str, start_date: str, end_date: str, max_retries: int = 3) -> pd.DataFrame:
        for attempt in range(max_retries):
            try:
                bars = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if not bars.empty:
                    return bars
                if attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                print(f"[WARN] Tushare index {index_name}({ts_code}) failed after {max_retries} retries: {exc}")
        return pd.DataFrame().sort_values(["index_code", "trade_date"])

    def _build_industry_daily(self, stock_basic: pd.DataFrame, stock_daily: pd.DataFrame) -> pd.DataFrame:
        return _build_industry_daily_from_frames(stock_basic, stock_daily)
        basics = stock_basic[["code", "industry"]].copy()
        merged = stock_daily.merge(basics, on="code", how="left")
        if merged.empty:
            raise RuntimeError("Cannot build industry_daily without stock bars")
        rows = []
        close_state: dict[str, float] = {}
        for (industry, trade_date), group in merged.groupby(["industry", "trade_date"], sort=True):
            prev = close_state.get(industry, 1000.0)
            pct_chg = float(group["pct_chg"].mean())
            close = max(100.0, prev * (1 + pct_chg / 100))
            rows.append(
                {
                    "industry": industry or "未分类",
                    "trade_date": trade_date,
                    "close": round(close, 2),
                    "pct_chg": round(pct_chg, 2),
                    "amount": round(float(group["amount"].sum()), 2),
                    "up_count": int((group["pct_chg"] > 0).sum()),
                    "down_count": int((group["pct_chg"] < 0).sum()),
                    "limit_up_count": int((group["pct_chg"] >= 9.5).sum()),
                }
            )
            close_state[industry] = close
        return pd.DataFrame(rows)

    def _fetch_financials(self, pro, stock_basic: pd.DataFrame) -> pd.DataFrame:
        return self._fetch_financials_by_period(pro, stock_basic)

    def _fetch_financials_by_period(self, pro, stock_basic: pd.DataFrame) -> pd.DataFrame:
        indicator_frames = []
        income_frames = []
        periods = _recent_report_periods(date.today())
        code_chunks = list(_chunks(stock_basic["ts_code"].astype(str).tolist(), _load_int_env("TUSHARE_FIN_CHUNK_SIZE", 50)))
        target_coverage = len(stock_basic) * 0.85
        for idx, period in enumerate(periods, start=1):
            self._emit(f"读取财务报告期：{period}", 84 + int(idx / max(len(periods), 1) * 14), 100)
            for chunk_idx, chunk in enumerate(code_chunks, start=1):
                if chunk_idx == 1 or chunk_idx % 20 == 0 or chunk_idx == len(code_chunks):
                    self._emit(
                        f"读取财务指标：{period}，分块 {chunk_idx}/{len(code_chunks)}",
                        84 + int(idx / max(len(periods), 1) * 14),
                        100,
                    )
                try:
                    raw = _tushare_call_with_retries(
                        pro.fina_indicator,
                        ts_code=",".join(chunk),
                        period=period,
                        fields="ts_code,end_date,profit_dedt,q_sales_yoy,debt_to_assets",
                    )
                except Exception:
                    raw = pd.DataFrame()
                if not raw.empty:
                    indicator_frames.append(raw)
                try:
                    income = _tushare_call_with_retries(
                        pro.income,
                        ts_code=",".join(chunk),
                        period=period,
                        fields="ts_code,end_date,n_income_attr_p",
                    )
                except Exception:
                    income = pd.DataFrame()
                if not income.empty:
                    income_frames.append(income)
            covered = set()
            if indicator_frames:
                covered |= set(pd.concat(indicator_frames, ignore_index=True)["ts_code"].astype(str).unique())
            if income_frames:
                covered |= set(pd.concat(income_frames, ignore_index=True)["ts_code"].astype(str).unique())
            if len(covered) >= target_coverage:
                break
        if not indicator_frames and not income_frames:
            return pd.DataFrame([_empty_financial_row(code) for code in stock_basic["code"]])

        self._emit("财务指标读取完成，正在整理财务字段", 98, 100)
        basic_codes = stock_basic[["ts_code", "code"]].copy()
        indicator = pd.concat(indicator_frames, ignore_index=True) if indicator_frames else pd.DataFrame(columns=["ts_code", "end_date"])
        income = pd.concat(income_frames, ignore_index=True) if income_frames else pd.DataFrame(columns=["ts_code", "end_date"])
        for col in ("profit_dedt", "q_sales_yoy", "debt_to_assets"):
            if col not in indicator.columns:
                indicator[col] = pd.NA
        if "n_income_attr_p" not in income.columns:
            income["n_income_attr_p"] = pd.NA
        if not indicator.empty:
            indicator = indicator.sort_values(["ts_code", "end_date"]).drop_duplicates("ts_code", keep="last")
        if not income.empty:
            income = income.sort_values(["ts_code", "end_date"]).drop_duplicates("ts_code", keep="last")
        raw = basic_codes.merge(indicator, on="ts_code", how="left").merge(
            income[["ts_code", "end_date", "n_income_attr_p"]].rename(columns={"end_date": "income_end_date"}),
            on="ts_code",
            how="left",
        )
        if raw.empty:
            return pd.DataFrame([_empty_financial_row(code) for code in stock_basic["code"]])
        raw["report_date_raw"] = raw["income_end_date"].fillna(raw["end_date"]).fillna("")
        raw["net_profit_missing"] = raw["n_income_attr_p"].isna().astype(int)
        raw["deducted_net_profit_missing"] = raw["profit_dedt"].isna().astype(int)
        financials = pd.DataFrame(
            {
                "code": raw["code"].astype(str),
                "report_date": raw["report_date_raw"].map(_format_tushare_date),
                "net_profit": raw["n_income_attr_p"].map(_to_float),
                "deducted_net_profit": raw["profit_dedt"].map(_to_float),
                "revenue_yoy": raw["q_sales_yoy"].map(_to_float),
                "asset_liability_ratio": raw["debt_to_assets"].map(lambda x: _to_float(x, default=100.0)),
                "net_profit_missing": raw["net_profit_missing"].astype(int),
                "deducted_net_profit_missing": raw["deducted_net_profit_missing"].astype(int),
            }
        )
        financials["data_quality_note"] = financials.apply(_financial_quality_note, axis=1)
        self._emit("补充财务摘要缺失字段", 99, 100)
        financials = _supplement_financials_with_akshare(financials, stock_basic)
        return financials


def _build_industry_daily_from_frames(stock_basic: pd.DataFrame, stock_daily: pd.DataFrame) -> pd.DataFrame:
    basics = stock_basic[["code", "industry"]].copy()
    merged = stock_daily.merge(basics, on="code", how="left")
    if merged.empty:
        raise RuntimeError("Cannot build industry_daily without stock bars")
    rows = []
    close_state: dict[str, float] = {}
    for (industry, trade_date), group in merged.groupby(["industry", "trade_date"], sort=True):
        industry_name = industry if isinstance(industry, str) and industry else "未分类"
        prev = close_state.get(industry_name, 1000.0)
        pct_chg = float(group["pct_chg"].mean())
        close = max(100.0, prev * (1 + pct_chg / 100))
        rows.append(
            {
                "industry": industry_name,
                "trade_date": trade_date,
                "close": round(close, 2),
                "pct_chg": round(pct_chg, 2),
                "amount": round(float(group["amount"].sum()), 2),
                "up_count": int((group["pct_chg"] > 0).sum()),
                "down_count": int((group["pct_chg"] < 0).sum()),
                "limit_up_count": int((group["pct_chg"] >= 9.5).sum()),
            }
        )
        close_state[industry_name] = close
    return pd.DataFrame(rows)


class HybridDataProvider:
    source_name = "hybrid"

    def fetch(self) -> MarketDataset:
        try:
            return AKShareDataProvider().fetch()
        except Exception:
            return SampleDataProvider().fetch()


def save_dataset(conn, dataset: MarketDataset, progress_callback: ProgressCallback | None = None) -> None:
    if progress_callback:
        progress_callback("清理旧数据表", 0, 100)
    for table in ["stock_basic", "index_daily", "stock_daily", "industry_daily", "financials"]:
        conn.execute(f"DELETE FROM {table}")
    for table in ["market_score", "industry_score", "strategy_result", "watch_pool", "run_log"]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    if progress_callback:
        progress_callback("写入股票基础信息", 12, 100)
    stock_basic = dataset.stock_basic.drop(columns=["ts_code"], errors="ignore")
    bulk_insert_frame(conn, "stock_basic", stock_basic)
    if progress_callback:
        progress_callback("写入指数行情", 28, 100)
    bulk_insert_frame(conn, "index_daily", dataset.index_daily)
    if progress_callback:
        progress_callback("写入个股日线", 48, 100)
    bulk_insert_frame(conn, "stock_daily", dataset.stock_daily)
    if progress_callback:
        progress_callback("写入行业日线", 72, 100)
    bulk_insert_frame(conn, "industry_daily", dataset.industry_daily)
    if progress_callback:
        progress_callback("写入财务指标", 88, 100)
    bulk_insert_frame(conn, "financials", dataset.financials)
    conn.execute("DELETE FROM financials WHERE report_date = '' OR report_date IS NULL")
    latest_trade_date = ""
    if not dataset.stock_daily.empty:
        latest_trade_date = str(dataset.stock_daily["trade_date"].max())
    conn.execute(
        """
        INSERT INTO data_snapshot (id, data_source, loaded_at, stock_count, latest_trade_date)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            data_source=excluded.data_source,
            loaded_at=excluded.loaded_at,
            stock_count=excluded.stock_count,
            latest_trade_date=excluded.latest_trade_date
        """,
        (
            dataset.source_name,
            datetime.now().isoformat(timespec="seconds"),
            int(len(stock_basic)),
            latest_trade_date,
        ),
    )
    conn.commit()
    if progress_callback:
        progress_callback("入库完成", 100, 100)


def save_dataset_incremental(conn, dataset: MarketDataset, progress_callback: ProgressCallback | None = None) -> None:
    if progress_callback:
        progress_callback("更新股票基础信息", 5, 100)
    conn.execute("DELETE FROM stock_basic")
    bulk_insert_frame(conn, "stock_basic", dataset.stock_basic.drop(columns=["ts_code"], errors="ignore"))

    if progress_callback:
        progress_callback("增量写入指数行情", 20, 100)
    upsert_frame(conn, "index_daily", dataset.index_daily, ["index_code", "trade_date"])
    if not dataset.stock_daily.empty:
        if progress_callback:
            progress_callback(f"增量写入个股日线：{len(dataset.stock_daily)} 条", 42, 100)
        upsert_frame(conn, "stock_daily", dataset.stock_daily, ["code", "trade_date"])
    if not dataset.industry_daily.empty:
        if progress_callback:
            progress_callback("增量写入行业日线", 62, 100)
        upsert_frame(conn, "industry_daily", dataset.industry_daily, ["industry", "trade_date"])
    full_stock_basic = read_sql(conn, "SELECT * FROM stock_basic")
    full_stock_daily = read_sql(conn, "SELECT * FROM stock_daily")
    if not full_stock_basic.empty and not full_stock_daily.empty:
        if progress_callback:
            progress_callback("重建行业日线", 62, 100)
        rebuilt_industry_daily = _build_industry_daily_from_frames(full_stock_basic, full_stock_daily)
        conn.execute("DELETE FROM industry_daily")
        bulk_insert_frame(conn, "industry_daily", rebuilt_industry_daily)
    if progress_callback:
        progress_callback("更新财务指标", 80, 100)
    upsert_frame(conn, "financials", dataset.financials, ["code", "report_date"])
    conn.execute("DELETE FROM financials WHERE report_date = '' OR report_date IS NULL")

    for table in ["market_score", "industry_score", "strategy_result", "watch_pool", "run_log"]:
        conn.execute(f"DELETE FROM {table}")
    latest_trade_date = _latest_trade_date_from_db_or_dataset(conn, dataset)
    conn.execute(
        """
        INSERT INTO data_snapshot (id, data_source, loaded_at, stock_count, latest_trade_date)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            data_source=excluded.data_source,
            loaded_at=excluded.loaded_at,
            stock_count=excluded.stock_count,
            latest_trade_date=excluded.latest_trade_date
        """,
        (
            dataset.source_name,
            datetime.now().isoformat(timespec="seconds"),
            int(len(dataset.stock_basic)),
            latest_trade_date,
        ),
    )
    conn.commit()
    if progress_callback:
        progress_callback("增量刷新完成", 100, 100)


def _load_tushare_token() -> str | None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    return token or None


def _format_tushare_date(value: object) -> str:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _empty_financial_row(code: str) -> dict[str, object]:
    return {
        "code": code,
        "report_date": "",
        "net_profit": 0.0,
        "deducted_net_profit": 0.0,
        "revenue_yoy": 0.0,
        "asset_liability_ratio": 100.0,
        "net_profit_missing": 1,
        "deducted_net_profit_missing": 1,
        "data_quality_note": "财务数据缺失或代理字段不足",
    }


def _financial_quality_note(row: pd.Series) -> str:
    notes = []
    if int(row.get("net_profit_missing", 0)):
        notes.append("归母净利润字段缺失")
    if int(row.get("deducted_net_profit_missing", 0)):
        notes.append("扣非净利润字段缺失")
    if notes:
        return "财务数据缺失或代理字段不足：" + "、".join(notes)
    return ""


def _supplement_financials_with_akshare(financials: pd.DataFrame, stock_basic: pd.DataFrame) -> pd.DataFrame:
    missing_codes = set(
        financials[
            (financials["net_profit_missing"].astype(int) == 1)
            | (financials["deducted_net_profit_missing"].astype(int) == 1)
            | (financials["report_date"].astype(str) == "")
        ]["code"].astype(str)
    )
    if not missing_codes:
        return financials
    try:
        import akshare as ak
    except Exception:
        return financials

    supplemented = financials.copy()
    for code in sorted(missing_codes):
        try:
            raw = ak.stock_financial_abstract_ths(symbol=str(code), indicator="按报告期")
        except Exception:
            continue
        if raw is None or raw.empty:
            continue
        row = raw.iloc[0]
        idx = supplemented.index[supplemented["code"].astype(str) == str(code)]
        if len(idx) == 0:
            continue
        idx0 = idx[0]
        net_profit = _pick_numeric(row, ["归母净利润", "净利润", "净利润-归母", "归属于母公司所有者的净利润"])
        deducted = _pick_numeric(row, ["扣非净利润", "扣除非经常性损益后的净利润"])
        revenue_yoy = _pick_numeric(row, ["营业总收入同比增长率", "营业收入同比增长率", "营收同比"])
        debt_ratio = _pick_numeric(row, ["资产负债率"])
        report_date = _pick_text(row, ["报告期", "公告日期", "日期"])
        if report_date:
            supplemented.at[idx0, "report_date"] = _format_tushare_date(report_date)
        if net_profit is not None:
            supplemented.at[idx0, "net_profit"] = net_profit
            supplemented.at[idx0, "net_profit_missing"] = 0
        if deducted is not None:
            supplemented.at[idx0, "deducted_net_profit"] = deducted
            supplemented.at[idx0, "deducted_net_profit_missing"] = 0
        if revenue_yoy is not None:
            supplemented.at[idx0, "revenue_yoy"] = revenue_yoy
        if debt_ratio is not None:
            supplemented.at[idx0, "asset_liability_ratio"] = debt_ratio
        supplemented.at[idx0, "data_quality_note"] = _financial_quality_note(supplemented.loc[idx0])
    return supplemented


def _pick_numeric(row: pd.Series, names: list[str]) -> float | None:
    for name in names:
        if name not in row.index:
            continue
        value = row.get(name)
        if pd.isna(value):
            continue
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in {"", "--", "None", "nan"}:
            continue
        unit = 1.0
        if text.endswith("亿"):
            unit = 100_000_000.0
            text = text[:-1]
        elif text.endswith("万"):
            unit = 10_000.0
            text = text[:-1]
        try:
            return float(text) * unit
        except ValueError:
            continue
    return None


def _pick_text(row: pd.Series, names: list[str]) -> str:
    for name in names:
        if name in row.index and not pd.isna(row.get(name)):
            return str(row.get(name)).strip()
    return ""


def _mark_suspended_codes(
    stock_basic: pd.DataFrame,
    stock_daily: pd.DataFrame,
    suspended_codes: set[str],
    latest_trade_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    basics = stock_basic.copy()
    daily = stock_daily.copy()
    basics["is_suspended"] = basics["code"].astype(str).isin(suspended_codes).astype(int)
    if not suspended_codes:
        return basics, daily
    latest_mask = daily["trade_date"].astype(str).eq(latest_trade_date) & daily["code"].astype(str).isin(suspended_codes)
    daily.loc[latest_mask, "is_suspended"] = 1
    for code in suspended_codes - set(daily.loc[latest_mask, "code"].astype(str)):
        code_rows = daily.index[daily["code"].astype(str).eq(code)].tolist()
        if code_rows:
            daily.loc[code_rows[-1], "is_suspended"] = 1
    return basics, daily


def _infer_suspended_from_latest_bar(
    stock_basic: pd.DataFrame,
    stock_daily: pd.DataFrame,
    latest_trade_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    latest_codes = set(stock_daily.loc[stock_daily["trade_date"].astype(str).eq(latest_trade_date), "code"].astype(str))
    all_codes = set(stock_basic["code"].astype(str))
    inferred = all_codes - latest_codes
    return _mark_suspended_codes(stock_basic, stock_daily, inferred, latest_trade_date)


def _empty_stock_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "pct_chg",
            "turnover_rate",
            "is_suspended",
            "is_limit_up",
            "is_limit_down",
        ]
    )


def _empty_industry_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["industry", "trade_date", "close", "pct_chg", "amount", "up_count", "down_count", "limit_up_count"]
    )


def _normalize_eastmoney_stock_hist(hist: pd.DataFrame) -> pd.DataFrame:
    column_map = {
        "股票代码": "code",
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_chg",
        "换手率": "turnover_rate",
    }
    frame = hist.rename(columns=column_map).copy()
    required = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate"]
    for column in required:
        if column not in frame.columns:
            frame[column] = 0.0 if column not in {"code", "trade_date"} else ""
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    for column in ["open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return frame[required]


def _normalize_tencent_stock_hist(hist: pd.DataFrame, code: str) -> pd.DataFrame:
    if hist.empty:
        return _empty_stock_daily_frame()
    frame = hist.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "amount": "volume",
        }
    ).copy()
    frame["code"] = str(code).zfill(6)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["amount"] = frame["volume"] * frame["close"] * 100
    frame["pct_chg"] = frame["close"].pct_change().fillna(0.0) * 100
    frame["turnover_rate"] = 0.0
    return frame[["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate"]]


def _normalize_tencent_index_hist(
    hist: pd.DataFrame,
    index_code: str,
    index_name: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frame = hist.copy()
    frame["trade_date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    start = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)].copy()
    if frame.empty:
        return pd.DataFrame(columns=["index_code", "index_name", "trade_date", "open", "high", "low", "close", "amount"])
    for column in ["open", "high", "low", "close", "amount"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["index_code"] = index_code
    frame["index_name"] = index_name
    frame["amount"] = frame["amount"] * 100
    return frame[["index_code", "index_name", "trade_date", "open", "high", "low", "close", "amount"]]


def _tencent_stock_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _latest_trade_date_from_db_or_dataset(conn, dataset: MarketDataset) -> str:
    if not dataset.stock_daily.empty:
        return str(dataset.stock_daily["trade_date"].max())
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_daily").fetchone()
    return str(row["trade_date"] or "")


def _recent_report_periods(today: date) -> list[str]:
    periods = []
    quarter_ends = ["1231", "0930", "0630", "0331"]
    for year in range(today.year, today.year - 4, -1):
        for suffix in quarter_ends:
            period = f"{year}{suffix}"
            if period <= today.strftime("%Y%m%d"):
                periods.append(period)
    return periods


def _chunks(values: list[str], size: int):
    size = max(1, size)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _load_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _tushare_call_with_retries(func, retries: int = 3, delay_seconds: float = 1.5, **kwargs):
    last_error = None
    for attempt in range(retries):
        try:
            return func(**kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(delay_seconds * (attempt + 1))
    raise last_error


def _akshare_call_with_retries(func, retries: int = 3, delay_seconds: float = 1.0, **kwargs):
    last_error = None
    for attempt in range(retries):
        try:
            return func(**kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(delay_seconds * (attempt + 1))
    raise last_error

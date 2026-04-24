"""
Data Fetcher - 数据采集调度器
整合多数据源（yfinance 主 / LongPort 实盘），数据清洗后存入本地缓存。

数据源策略（2026-04-21 起）：
- 历史回测：默认 yfinance（量大、稳定、无配额限制）
- 实时行情 / 账户 / 交易：LongPort
"""

from datetime import datetime, timedelta
from typing import List, Optional, Union

import pandas as pd
from loguru import logger

from src.data.longport_client import LongPortClient
from src.data.yfinance_client import YFinanceClient, YFINANCE_AVAILABLE
from src.data.database import DatabaseManager
from src.data.market_calendar import USMarketCalendar


# 能同时提供 get_history_kline 的客户端接口类型
HistoryClient = Union[LongPortClient, YFinanceClient]


class DataFetcher:
    """数据采集调度器。

    Args:
        client: 历史数据客户端（默认 yfinance；回退 LongPort）
        db: 数据库管理器
        calendar: 交易日历
        history_source: "yfinance" | "longport"，指定历史数据源。默认 yfinance。
    """

    def __init__(
        self,
        client: Optional[HistoryClient] = None,
        db: Optional[DatabaseManager] = None,
        calendar: Optional[USMarketCalendar] = None,
        history_source: str = "yfinance",
    ):
        self.history_source = history_source

        if client is not None:
            self.client = client
        elif history_source == "yfinance" and YFINANCE_AVAILABLE:
            self.client = YFinanceClient()
            logger.info("DataFetcher using yfinance as history source")
        else:
            self.client = LongPortClient()
            logger.info("DataFetcher using LongPort as history source")

        self.db = db or DatabaseManager()
        self.calendar = calendar or USMarketCalendar()

    # ----------------------------------------------------------
    # Historical Data Fetch
    # ----------------------------------------------------------

    def fetch_history(
        self,
        symbols: List[str],
        period: str = "1d",
        count: int = 250,
        adjust: str = "qfq",
        save_to_db: bool = True,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        """
        批量下载历史K线数据。

        Args:
            symbols: 标的代码列表
            period: K线周期
            count: 每个标的获取的K线数量（start/end 指定时忽略）
            adjust: 复权方式
            save_to_db: 是否保存到数据库
            start_date / end_date: 可选日期范围（yfinance 支持；LongPort 忽略）

        Returns:
            dict: {symbol: DataFrame}
        """
        results = {}
        total = len(symbols)

        # 构造透传给 client 的 kwargs（YFinanceClient 额外支持 start_date/end_date）
        extra_kwargs = {}
        if isinstance(self.client, YFinanceClient) and (start_date or end_date):
            extra_kwargs["start_date"] = start_date
            extra_kwargs["end_date"] = end_date

        for i, symbol in enumerate(symbols, 1):
            try:
                logger.info(f"[{i}/{total}] Fetching {symbol} history ({period})...")
                df = self.client.get_history_kline(
                    symbol=symbol,
                    period=period,
                    count=count,
                    adjust=adjust,
                    **extra_kwargs,
                )

                if df.empty:
                    logger.warning(f"No data returned for {symbol}")
                    continue

                # 数据清洗
                df = self._clean_kline_data(df)

                if save_to_db:
                    self.db.save_kline(symbol, df, period, adjust)

                results[symbol] = df
                logger.info(
                    f"  -> {len(df)} bars, "
                    f"range: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}"
                )
            except Exception as e:
                logger.error(f"Failed to fetch {symbol}: {e}")

        logger.info(f"History fetch complete: {len(results)}/{total} symbols")
        return results

    def fetch_realtime(self, symbols: List[str]) -> pd.DataFrame:
        """
        获取实时行情快照。强制走 LongPort（yfinance 不提供实时）。

        Returns:
            DataFrame with realtime quotes
        """
        try:
            client = self._get_longport_client()
            df = client.get_realtime_quote(symbols)
            logger.debug(f"Realtime quotes fetched for {len(symbols)} symbols")
            return df
        except Exception as e:
            logger.error(f"Failed to fetch realtime quotes: {e}")
            return pd.DataFrame()

    def _get_longport_client(self) -> LongPortClient:
        """返回 LongPort 客户端（用于 realtime/account 等非 yfinance 能力）。"""
        if isinstance(self.client, LongPortClient):
            return self.client
        # 历史源是 yfinance 时，按需懒加载 LongPort
        if not hasattr(self, "_longport_client") or self._longport_client is None:
            self._longport_client = LongPortClient()
        return self._longport_client

    # ----------------------------------------------------------
    # Data Loading (from local cache)
    # ----------------------------------------------------------

    def load_data(
        self,
        symbol: str,
        period: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        从本地缓存加载K线数据。
        如果本地无数据则自动从 API 拉取。
        
        Returns:
            DataFrame with OHLCV columns
        """
        df = self.db.load_kline(symbol, period, start_date, end_date, adjust)

        if df.empty:
            logger.info(f"No local data for {symbol}, fetching from API...")
            result = self.fetch_history(
                [symbol], period=period, adjust=adjust
            )
            df = result.get(symbol, pd.DataFrame())

            # 再次过滤日期范围
            if not df.empty and start_date:
                df = df[df["date"] >= start_date]
            if not df.empty and end_date:
                df = df[df["date"] <= end_date]

        return df

    # ----------------------------------------------------------
    # Account Data
    # ----------------------------------------------------------

    def fetch_account_info(self) -> dict:
        """
        获取账户信息汇总：资金 + 持仓。强制走 LongPort。

        Returns:
            dict with keys: balances, positions
        """
        try:
            client = self._get_longport_client()
            balances = client.get_account_balance()
            positions = client.get_positions()
            return {"balances": balances, "positions": positions}
        except Exception as e:
            logger.error(f"Failed to fetch account info: {e}")
            return {"balances": [], "positions": pd.DataFrame()}

    # ----------------------------------------------------------
    # Data Cleaning
    # ----------------------------------------------------------

    @staticmethod
    def _clean_kline_data(df: pd.DataFrame) -> pd.DataFrame:
        """
        K线数据清洗。
        - 去重
        - 排序
        - 处理异常值
        """
        if df.empty:
            return df

        # 去重
        df = df.drop_duplicates(subset=["date"], keep="last")

        # 按日期排序
        df = df.sort_values("date").reset_index(drop=True)

        # 过滤明显异常值（价格<=0, 成交量<0）
        df = df[(df["close"] > 0) & (df["volume"] >= 0)]

        return df.reset_index(drop=True)

    # ----------------------------------------------------------
    # Data Export
    # ----------------------------------------------------------

    def export_to_csv(
        self,
        symbol: str,
        output_dir: str = "data_cache/csv",
        period: str = "1d",
    ) -> str:
        """
        导出K线数据为 CSV 文件。
        
        Returns:
            CSV 文件路径
        """
        import os

        os.makedirs(output_dir, exist_ok=True)
        df = self.load_data(symbol, period)

        if df.empty:
            logger.warning(f"No data to export for {symbol}")
            return ""

        filename = f"{symbol.replace('.', '_')}_{period}.csv"
        filepath = os.path.join(output_dir, filename)
        df.to_csv(filepath, index=False)
        logger.info(f"Exported {len(df)} rows to {filepath}")
        return filepath

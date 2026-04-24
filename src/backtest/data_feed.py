"""
Custom Data Feed - 自定义 backtrader DataFeed
将本地 SQLite/CSV/DataFrame 数据适配为 backtrader 可识别的格式。
"""

import datetime
from typing import Optional

import pandas as pd

try:
    import backtrader as bt

    BACKTRADER_AVAILABLE = True
except ImportError:
    BACKTRADER_AVAILABLE = False


if BACKTRADER_AVAILABLE:

    class PandasDataFeed(bt.feeds.PandasData):
        """
        基于 Pandas DataFrame 的 backtrader DataFeed。
        
        接受的 DataFrame 格式:
        - columns: date, open, high, low, close, volume
        - date 列为 datetime 类型
        - 按日期升序排列
        """

        params = (
            ("datetime", "date"),
            ("open", "open"),
            ("high", "high"),
            ("low", "low"),
            ("close", "close"),
            ("volume", "volume"),
            ("openinterest", -1),  # 无持仓量数据
        )

    class CSVDataFeed(bt.feeds.GenericCSVData):
        """
        基于 CSV 文件的 backtrader DataFeed。
        
        CSV 格式要求:
        date,open,high,low,close,volume
        2024-01-02,150.50,152.30,149.80,151.00,50000000
        """

        params = (
            ("dtformat", "%Y-%m-%d"),
            ("datetime", 0),
            ("open", 1),
            ("high", 2),
            ("low", 3),
            ("close", 4),
            ("volume", 5),
            ("openinterest", -1),
        )


def create_data_feed(
    data: pd.DataFrame,
    name: str = "",
    fromdate: Optional[datetime.datetime] = None,
    todate: Optional[datetime.datetime] = None,
):
    """
    从 DataFrame 创建 backtrader DataFeed。
    
    Args:
        data: OHLCV DataFrame
        name: 数据源名称（标的代码）
        fromdate: 起始日期
        todate: 结束日期
        
    Returns:
        backtrader DataFeed 实例
    """
    if not BACKTRADER_AVAILABLE:
        raise RuntimeError("backtrader not installed. Run: pip install backtrader")

    df = data.copy()

    # 确保 date 列是 datetime 类型
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    
    # 确保必须的列存在
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    kwargs = {
        "dataname": df,
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "openinterest": None,
    }
    if fromdate:
        kwargs["fromdate"] = fromdate
    if todate:
        kwargs["todate"] = todate

    feed = bt.feeds.PandasData(**kwargs)
    if name:
        feed._name = name

    return feed

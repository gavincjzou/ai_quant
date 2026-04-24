"""
Data Module Tests - 数据模块测试
验证数据存储、查询、市场日历等功能。
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.database import DatabaseManager
from src.data.market_calendar import USMarketCalendar


class TestDatabaseManager(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = DatabaseManager(self.db_path)

    def test_save_and_load_kline(self):
        """保存和加载K线数据"""
        df = pd.DataFrame({
            "date": pd.date_range("2024-01-02", periods=5, freq="B"),
            "open": [150.0, 151.0, 152.0, 153.0, 154.0],
            "high": [151.0, 152.0, 153.0, 154.0, 155.0],
            "low": [149.0, 150.0, 151.0, 152.0, 153.0],
            "close": [150.5, 151.5, 152.5, 153.5, 154.5],
            "volume": [1000000] * 5,
            "turnover": [0] * 5,
        })
        self.db.save_kline("AAPL.US", df, "1d", "qfq")

        loaded = self.db.load_kline("AAPL.US", "1d")
        self.assertEqual(len(loaded), 5)
        self.assertAlmostEqual(loaded["close"].iloc[0], 150.5)

    def test_save_trade(self):
        """保存交易记录"""
        trade = {
            "order_id": "test_001",
            "trade_mode": "paper",
            "symbol": "AAPL.US",
            "side": "buy",
            "quantity": 10,
            "price": 150.0,
            "commission": 0.99,
            "strategy_name": "ma_cross",
            "executed_at": "2024-01-02T10:00:00",
        }
        self.db.save_trade(trade)

        trades = self.db.load_trades("paper")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0]["symbol"], "AAPL.US")

    def test_save_backtest_result(self):
        """保存回测结果"""
        result = {
            "strategy_name": "ma_cross",
            "symbol": "AAPL.US",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 10000,
            "final_value": 12000,
            "total_return": 0.20,
            "annual_return": 0.20,
            "max_drawdown": 0.05,
            "sharpe_ratio": 1.5,
            "win_rate": 0.6,
            "profit_loss_ratio": 2.0,
            "trade_count": 20,
        }
        self.db.save_backtest_result(result)

        results = self.db.load_backtest_results("ma_cross")
        self.assertEqual(len(results), 1)


class TestUSMarketCalendar(unittest.TestCase):

    def setUp(self):
        self.cal = USMarketCalendar()

    def test_weekday_is_trading(self):
        """工作日应为交易日（除节假日外）"""
        # 2024-01-02 是周二
        self.assertTrue(self.cal.is_trading_day(date(2024, 1, 2)))

    def test_weekend_not_trading(self):
        """周末不是交易日"""
        # 2024-01-06 是周六
        self.assertFalse(self.cal.is_trading_day(date(2024, 1, 6)))
        # 2024-01-07 是周日
        self.assertFalse(self.cal.is_trading_day(date(2024, 1, 7)))

    def test_holiday_not_trading(self):
        """节假日不是交易日"""
        # 2024-12-25 圣诞节
        self.assertFalse(self.cal.is_trading_day(date(2024, 12, 25)))

    def test_next_trading_day(self):
        """获取下一个交易日"""
        # 从周五开始，下一个交易日应该是周一
        friday = date(2024, 1, 5)
        next_td = self.cal.next_trading_day(friday)
        self.assertEqual(next_td.weekday(), 0)  # Monday

    def test_get_trading_days_range(self):
        """获取日期范围内的交易日"""
        days = self.cal.get_trading_days(date(2024, 1, 1), date(2024, 1, 7))
        for d in days:
            self.assertTrue(self.cal.is_trading_day(d))
            self.assertLess(d.weekday(), 5)


if __name__ == "__main__":
    unittest.main()

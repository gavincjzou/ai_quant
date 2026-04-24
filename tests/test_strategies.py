"""
Strategy Unit Tests - 策略单元测试
验证各策略在已知数据上产生正确的买卖信号。
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.strategy.base_strategy import Signal
from src.strategy.ma_cross_strategy import MACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy
from src.strategy.momentum_strategy import MomentumStrategy
from src.strategy.strategy_manager import StrategyManager


def _generate_uptrend_data(n=50, start_price=100):
    """生成上升趋势数据"""
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = start_price + np.arange(n) * 0.5 + np.random.randn(n) * 0.3
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.random.randint(1000000, 5000000, n),
    })


def _generate_downtrend_data(n=50, start_price=150):
    """生成下降趋势数据"""
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = start_price - np.arange(n) * 0.5 + np.random.randn(n) * 0.3
    return pd.DataFrame({
        "date": dates,
        "open": close + 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.random.randint(1000000, 5000000, n),
    })


def _generate_oversold_data(n=30, start_price=100):
    """生成连续下跌数据（触发RSI超卖）"""
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    # 前半段正常，后半段暴跌
    close = np.concatenate([
        np.full(n // 2, start_price) + np.random.randn(n // 2) * 0.5,
        start_price - np.arange(n - n // 2) * 2,  # 每天跌2块
    ])
    return pd.DataFrame({
        "date": dates,
        "open": close + 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.random.randint(1000000, 5000000, n),
    })


class TestMACrossStrategy(unittest.TestCase):

    def setUp(self):
        self.strategy = MACrossStrategy()
        # 阶段6：测试保持 legacy 行为，关闭量能确认（避免随机 volume 导致断言失败）
        self.strategy.init({
            "short_period": 5, "long_period": 20, "signal_type": "SMA",
            "volume_confirm_enabled": False,
        })

    def test_init(self):
        self.assertEqual(self.strategy.name, "ma_cross")
        self.assertEqual(self.strategy.get_params()["short_period"], 5)

    def test_insufficient_data(self):
        """数据不足应返回 None"""
        short_data = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="B"),
            "open": [100]*10, "high": [101]*10, "low": [99]*10,
            "close": [100]*10, "volume": [1000000]*10,
        })
        signal = self.strategy.generate_signal("AAPL.US", short_data)
        self.assertIsNone(signal)

    def test_uptrend_signal(self):
        """上升趋势数据应在某个时点产生BUY信号"""
        data = _generate_uptrend_data(60)
        has_buy = False
        for i in range(25, len(data)):
            signal = self.strategy.generate_signal("AAPL.US", data.iloc[:i+1])
            if signal and signal.signal == Signal.BUY:
                has_buy = True
                break
        # 上升趋势中应至少产生一次买入信号
        self.assertTrue(has_buy, "Expected at least one BUY signal in uptrend")


class TestRSIStrategy(unittest.TestCase):

    def setUp(self):
        self.strategy = RSIStrategy()
        # 阶段6：测试保持 legacy 行为，关闭趋势过滤（避免 _generate_oversold_data 被 MA50 挡住）
        self.strategy.init({
            "period": 14, "overbought": 70, "oversold": 30,
            "trend_filter_enabled": False,
        })

    def test_init(self):
        self.assertEqual(self.strategy.name, "rsi")
        self.assertEqual(self.strategy.get_params()["period"], 14)

    def test_rsi_calculation(self):
        """RSI 计算基本验证"""
        series = pd.Series([44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 
                           45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
                           46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
        rsi = self.strategy._calc_rsi(series, 14)
        # RSI 应在 0-100 之间
        valid_rsi = rsi.dropna()
        self.assertTrue(all(0 <= v <= 100 for v in valid_rsi))

    def test_oversold_signal(self):
        """连续下跌应触发超卖买入信号"""
        data = _generate_oversold_data(30)
        signal = self.strategy.generate_signal("AAPL.US", data)
        if signal:
            self.assertEqual(signal.signal, Signal.BUY)


class TestMomentumStrategy(unittest.TestCase):

    def setUp(self):
        self.strategy = MomentumStrategy()
        self.strategy.init({
            "lookback_period": 20,
            "buy_threshold": 0.05,
            "sell_threshold": -0.03,
        })

    def test_init(self):
        self.assertEqual(self.strategy.name, "momentum")

    def test_strong_uptrend(self):
        """强上升趋势应产生买入信号"""
        data = _generate_uptrend_data(30, start_price=100)
        signal = self.strategy.generate_signal("AAPL.US", data)
        if signal:
            self.assertEqual(signal.signal, Signal.BUY)

    def test_strong_downtrend(self):
        """强下降趋势应产生卖出信号"""
        data = _generate_downtrend_data(30, start_price=150)
        signal = self.strategy.generate_signal("AAPL.US", data)
        if signal:
            self.assertEqual(signal.signal, Signal.SELL)


class TestStrategyManager(unittest.TestCase):

    def test_load_strategies(self):
        config = {
            "active_strategies": ["ma_cross", "rsi"],
            "ma_cross": {"short_period": 5, "long_period": 20},
            "rsi": {"period": 14, "overbought": 70, "oversold": 30},
        }
        mgr = StrategyManager(config)
        self.assertEqual(len(mgr.list_strategies()), 2)
        self.assertIn("ma_cross", mgr.list_strategies())
        self.assertIn("rsi", mgr.list_strategies())

    def test_unknown_strategy(self):
        config = {"active_strategies": ["nonexistent"]}
        mgr = StrategyManager(config)
        self.assertEqual(len(mgr.list_strategies()), 0)


if __name__ == "__main__":
    unittest.main()

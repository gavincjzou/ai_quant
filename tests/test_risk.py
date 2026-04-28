"""
Risk Module Tests - 风控模块测试
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.risk_manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.risk.stop_loss import StopLossManager
from src.strategy.base_strategy import Signal, TradeSignal


SAMPLE_RISK_CONFIG = {
    "position": {
        "max_single_position_pct": 0.20,
        "default_position_pct": 0.10,
        "max_total_positions": 5,
        "min_order_amount_usd": 50,
    },
    "stop_loss": {
        "per_trade_stop_loss_pct": 0.05,
        "per_trade_take_profit_pct": 0.15,
        "trailing_stop_enabled": True,
        "trailing_stop_pct": 0.05,
    },
    "daily_limits": {
        "max_daily_loss_pct": 0.03,
        "max_daily_trades": 10,
    },
    "portfolio_limits": {
        "max_drawdown_pct": 0.10,
        "block_earnings_window_days": 1,
    },
}


class TestRiskManager(unittest.TestCase):

    def setUp(self):
        self.rm = RiskManager(SAMPLE_RISK_CONFIG)

    def test_buy_signal_passes(self):
        """正常买入信号应通过风控"""
        signal = TradeSignal(
            symbol="AAPL.US", signal=Signal.BUY, price=150.0, strategy_name="test"
        )
        account = {"total_assets": 10000, "available_cash": 10000}
        result = self.rm.check_order(signal, account, {})
        self.assertTrue(result.passed)
        self.assertGreater(result.approved_quantity, 0)

    def test_sell_signal_always_passes(self):
        """卖出信号应直接通过"""
        signal = TradeSignal(
            symbol="AAPL.US", signal=Signal.SELL, price=150.0, quantity=10, strategy_name="test"
        )
        account = {"total_assets": 10000, "available_cash": 0}
        positions = {"AAPL.US": {"quantity": 10, "market_value": 1500}}
        result = self.rm.check_order(signal, account, positions)
        self.assertTrue(result.passed)

    def test_max_positions_limit(self):
        """超过最大持仓数量应被拦截"""
        signal = TradeSignal(
            symbol="NEW.US", signal=Signal.BUY, price=50.0, strategy_name="test"
        )
        account = {"total_assets": 50000, "available_cash": 10000}
        # 已有5个持仓
        positions = {f"STOCK{i}.US": {"quantity": 10, "market_value": 1000} for i in range(5)}
        result = self.rm.check_order(signal, account, positions)
        self.assertFalse(result.passed)

    def test_circuit_breaker(self):
        """回撤熔断应阻止所有交易"""
        self.rm._peak_value = 10000
        self.rm.update_portfolio_value(8000)  # 20% 回撤 > 10% 阈值
        self.assertTrue(self.rm._is_circuit_breaker)

        signal = TradeSignal(
            symbol="AAPL.US", signal=Signal.BUY, price=150.0, strategy_name="test"
        )
        account = {"total_assets": 8000, "available_cash": 8000}
        result = self.rm.check_order(signal, account, {})
        self.assertFalse(result.passed)


class TestPositionSizer(unittest.TestCase):

    def setUp(self):
        self.sizer = PositionSizer(SAMPLE_RISK_CONFIG["position"])

    def test_fixed_pct(self):
        """固定比例计算"""
        shares = self.sizer.calculate(
            price=150.0, total_assets=10000, available_cash=10000
        )
        # 10000 * 10% / 150 ≈ 6
        self.assertGreater(shares, 0)
        self.assertLessEqual(shares * 150, 10000 * 0.20)

    def test_zero_price(self):
        """价格为0应返回0"""
        shares = self.sizer.calculate(
            price=0, total_assets=10000, available_cash=10000
        )
        self.assertEqual(shares, 0)

    def test_kelly_mode(self):
        """Kelly 模式计算"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=10000, available_cash=10000,
            mode="kelly", win_rate=0.6, profit_loss_ratio=2.0,
        )
        self.assertGreater(shares, 0)


class TestStopLossManager(unittest.TestCase):

    def setUp(self):
        self.slm = StopLossManager(SAMPLE_RISK_CONFIG["stop_loss"])

    def test_stop_loss_trigger(self):
        """跌破止损线应触发卖出"""
        # 签名修正：track_position(symbol, entry_price, size)
        self.slm.track_position("AAPL.US", 150.0, 10)
        self.slm.update_price("AAPL.US", 141.0)  # 跌6% > 5%止损线
        signals = self.slm.check_all()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal, Signal.SELL)

    def test_take_profit_trigger(self):
        """达到止盈线应触发卖出"""
        self.slm.track_position("AAPL.US", 100.0, 10)
        self.slm.update_price("AAPL.US", 116.0)  # 涨16% > 15%止盈线
        signals = self.slm.check_all()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal, Signal.SELL)

    def test_trailing_stop(self):
        """追踪止损：从最高价回落超过阈值。

        注意：legacy 模式下，价格涨到 +15% 会先触发 take_profit，
        所以追踪止损测试用更保守的高点（不超过 +15%）。
        """
        self.slm.track_position("AAPL.US", 100.0, 10)
        self.slm.update_price("AAPL.US", 113.0)  # 新高 +13%（不触发止盈）
        self.slm.update_price("AAPL.US", 107.0)  # 从高点回落 5.3% > 5%
        signals = self.slm.check_all()
        self.assertGreaterEqual(len(signals), 1)
        # 任一信号含 trailing 即可（实现可能是 "Trailing stop" 或 "trailing"）
        reasons = " ".join(s.reason or "" for s in signals).lower()
        self.assertIn("trailing", reasons)

    def test_no_trigger(self):
        """正常波动不应触发"""
        self.slm.track_position("AAPL.US", 100.0, 10)
        self.slm.update_price("AAPL.US", 102.0)  # 涨2%
        signals = self.slm.check_all()
        self.assertEqual(len(signals), 0)


if __name__ == "__main__":
    unittest.main()

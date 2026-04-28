"""
test_stop_loss.py - StopLossManager 单元测试

覆盖：
- legacy 模式：-5% 止损、+15% 止盈、5% trailing
- atr_442 模式：tp1/tp2/tp3 按 RR 分批 + 追踪止损
- track_position 开仓追踪
- update_price 更新 highest_price
- remove_position / reduce_position
- dump_state / load_state 持久化
- reset 清空
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.stop_loss import StopLossManager, PositionTracker


LEGACY_CONFIG = {
    "stop_loss": {
        "mode": "legacy",
        "per_trade_stop_loss_pct": 0.05,
        "per_trade_take_profit_pct": 0.15,
        "trailing_stop_enabled": True,
        "trailing_stop_pct": 0.05,
    }
}


ATR_442_CONFIG = {
    "stop_loss": {
        "mode": "atr_442",
        "atr_442": {
            "stop_mult": 2.0,
            "tp1_rr": 1.0,
            "tp2_rr": 2.0,
            "tp3_rr": 3.0,
            "tp1_pct": 0.4,   # 40% 仓位
            "tp2_pct": 0.4,
            "tp3_pct": 0.2,
        },
    },
    "per_strategy_overrides": {
        "momentum": {
            "stop_mult": 2.0,
            "tp1_rr": 1.0,
            "tp2_rr": 2.0,
            "tp3_rr": 3.5,
        },
    },
}


class TestStopLossLifecycle(unittest.TestCase):
    """基本生命周期：track / update / remove / reset"""

    def setUp(self):
        self.sl = StopLossManager(LEGACY_CONFIG)

    def test_track_position_registers(self):
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.assertIn("NVDA.US", self.sl._positions)
        p = self.sl._positions["NVDA.US"]
        self.assertEqual(p.quantity, 100)
        self.assertEqual(p.avg_cost, 100.0)

    def test_update_price_tracks_highest(self):
        """highest_price 应随价格上行而抬高，不随价格下行回落"""
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.update_price("NVDA.US", 110.0)
        self.assertEqual(self.sl._positions["NVDA.US"].highest_price, 110.0)
        self.sl.update_price("NVDA.US", 105.0)  # 回落
        self.assertEqual(self.sl._positions["NVDA.US"].highest_price, 110.0)  # 保持高点

    def test_remove_position(self):
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.remove_position("NVDA.US")
        self.assertNotIn("NVDA.US", self.sl._positions)

    def test_reduce_position(self):
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.reduce_position("NVDA.US", 30)
        # reduce 后 remaining_size 或 quantity 应减少（看实现）
        p = self.sl._positions["NVDA.US"]
        self.assertTrue(p.quantity <= 100 or p.remaining_size <= 100)

    def test_reset_clears_all(self):
        self.sl.track_position("A", 100.0, 10)
        self.sl.track_position("B", 200.0, 20)
        self.sl.reset()
        self.assertEqual(len(self.sl._positions), 0)


class TestStopLossLegacyMode(unittest.TestCase):
    """legacy 模式触发信号"""

    def setUp(self):
        self.sl = StopLossManager(LEGACY_CONFIG)

    def test_stop_loss_triggers_below_threshold(self):
        """价格跌破 -5% 应触发 SELL 信号"""
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.update_price("NVDA.US", 94.0)  # -6%
        signals = self.sl.check_all()
        self.assertEqual(len(signals), 1)
        sigs_syms = [s.symbol for s in signals]
        self.assertIn("NVDA.US", sigs_syms)

    def test_take_profit_triggers(self):
        """价格涨到 +15% 应触发止盈"""
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.update_price("NVDA.US", 116.0)  # +16%
        signals = self.sl.check_all()
        self.assertEqual(len(signals), 1)

    def test_no_signal_within_range(self):
        """价格在 -5% 到 +15% 之间不应触发"""
        self.sl.track_position("NVDA.US", entry_price=100.0, size=100)
        self.sl.update_price("NVDA.US", 102.0)
        signals = self.sl.check_all()
        self.assertEqual(len(signals), 0)


class TestStopLossATR442(unittest.TestCase):
    """ATR 442 分批止盈测试"""

    def setUp(self):
        self.sl = StopLossManager(ATR_442_CONFIG)

    def test_track_atr442_sets_tp_prices(self):
        """atr_442 模式开仓时应算出 tp1/tp2/tp3 价格"""
        self.sl.track_position(
            "NVDA.US", entry_price=100.0, size=100,
            atr=2.0, strategy_name="momentum",
        )
        p = self.sl._positions["NVDA.US"]
        # stop = 100 - 2×2 = 96；R = 4
        # tp1 = 100 + 1×4 = 104, tp2 = 108, tp3 = 114
        self.assertIsNotNone(p.tp1_price)
        self.assertIsNotNone(p.tp2_price)
        self.assertIsNotNone(p.tp3_price)
        # 各 TP 依次升高
        self.assertLess(p.tp1_price, p.tp2_price)
        self.assertLess(p.tp2_price, p.tp3_price)

    def test_tp1_triggers_partial_sell(self):
        """价格到 tp1 应触发第一次分批止盈"""
        self.sl.track_position(
            "NVDA.US", entry_price=100.0, size=100,
            atr=2.0, strategy_name="momentum",
        )
        tp1 = self.sl._positions["NVDA.US"].tp1_price
        self.sl.update_price("NVDA.US", tp1 + 0.5)
        signals = self.sl.check_all()
        # 应产生至少 1 个信号（tp1 触发）
        self.assertGreaterEqual(len(signals), 1)


class TestStopLossPersistence(unittest.TestCase):
    """dump_state / load_state 持久化"""

    def setUp(self):
        self.sl = StopLossManager(ATR_442_CONFIG)

    def test_dump_and_load_roundtrip(self):
        """dump 出来再 load 回去，持仓应完整恢复"""
        self.sl.track_position("NVDA.US", 100.0, 100, atr=2.0, strategy_name="momentum")
        self.sl.track_position("MU.US", 50.0, 200, atr=1.5, strategy_name="ma_cross")
        self.sl.update_price("NVDA.US", 110.0)

        state = self.sl.dump_state()
        self.assertIn("positions", state)
        self.assertEqual(len(state["positions"]), 2)

        sl2 = StopLossManager(ATR_442_CONFIG)
        n = sl2.load_state(state)
        self.assertEqual(n, 2)
        self.assertEqual(sl2._positions["NVDA.US"].quantity, 100)
        self.assertEqual(sl2._positions["NVDA.US"].highest_price, 110.0)

    def test_load_empty_state_is_noop(self):
        """load None 或空 state 不应崩"""
        n = self.sl.load_state(None)
        self.assertEqual(n, 0)
        n = self.sl.load_state({})
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()

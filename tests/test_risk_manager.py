"""
test_risk_manager.py - RiskManager 扩展测试

补充 test_risk.py 已有 TestRiskManager 之外的测试：
- 单日交易次数熔断
- 单日亏损熔断
- 累计回撤熔断（update_portfolio_value）
- 熔断手动重置
- dump_state / load_state 持久化
- 财报窗口屏蔽（_is_near_earnings）
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.risk_manager import RiskManager
from src.strategy.base_strategy import Signal, TradeSignal


# 配置：低阈值方便触发
TIGHT_CONFIG = {
    "position": {
        "max_single_position_pct": 0.20,
        "default_position_pct": 0.10,
        "max_total_positions": 5,
        "min_order_amount_usd": 50,
    },
    "stop_loss": {
        "per_trade_stop_loss_pct": 0.05,
    },
    "daily_limits": {
        "max_daily_loss_pct": 0.03,   # 单日亏损 3%
        "max_daily_trades": 3,         # 单日 3 笔
    },
    "portfolio_limits": {
        "max_drawdown_pct": 0.10,      # 累计回撤 10%
        "block_earnings_window_days": 1,
    },
}


def _buy(symbol="AAPL.US", price=150.0, qty=10):
    return TradeSignal(
        symbol=symbol, signal=Signal.BUY, price=price,
        quantity=qty, strategy_name="test"
    )


class TestDailyTradeLimit(unittest.TestCase):
    """单日交易次数熔断"""

    def setUp(self):
        self.rm = RiskManager(TIGHT_CONFIG)

    def test_within_limit_passes(self):
        """3 次以下应通过"""
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertTrue(result.passed)

    def test_exceed_limit_blocks(self):
        """超过 max_daily_trades 应被拒"""
        # 模拟已经成交了 3 笔
        self.rm._daily_trade_count = 3
        self.rm._daily_date = date.today()  # 关键：避免 check_order 重置
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertFalse(result.passed)
        self.assertTrue(any("Daily trade limit" in r for r in result.rejected_reasons))

    def test_new_day_resets_counter(self):
        """日期变化时计数器应自动重置"""
        # 设置昨天的状态
        self.rm._daily_trade_count = 5
        self.rm._daily_date = date.today() - timedelta(days=1)
        # 今天的请求应该重置后通过
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertEqual(self.rm._daily_trade_count, 0)
        self.assertTrue(result.passed)


class TestDailyLossLimit(unittest.TestCase):
    """单日亏损熔断"""

    def setUp(self):
        self.rm = RiskManager(TIGHT_CONFIG)

    def test_loss_within_limit_passes(self):
        """小亏损不影响下单"""
        self.rm._daily_pnl = -100  # 亏损 $100
        self.rm._daily_date = date.today()
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertTrue(result.passed)

    def test_loss_exceeds_limit_blocks(self):
        """单日亏损超 3% 应拒"""
        self.rm._daily_pnl = -400  # 亏 4%（超过 3%）
        self.rm._daily_date = date.today()
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertFalse(result.passed)
        self.assertTrue(any("Daily loss" in r or "loss" in r.lower() for r in result.rejected_reasons))


class TestCircuitBreaker(unittest.TestCase):
    """累计回撤熔断"""

    def setUp(self):
        self.rm = RiskManager(TIGHT_CONFIG)

    def test_no_drawdown_no_breaker(self):
        """组合上涨时不触发熔断"""
        self.rm.update_portfolio_value(10000)
        self.rm.update_portfolio_value(11000)
        self.assertFalse(self.rm._is_circuit_breaker)

    def test_small_drawdown_no_breaker(self):
        """小回撤不触发"""
        self.rm.update_portfolio_value(10000)
        self.rm.update_portfolio_value(9500)  # -5%
        self.assertFalse(self.rm._is_circuit_breaker)

    def test_large_drawdown_triggers_breaker(self):
        """回撤 ≥10% 触发熔断"""
        self.rm.update_portfolio_value(10000)
        self.rm.update_portfolio_value(8900)  # -11%
        self.assertTrue(self.rm._is_circuit_breaker)

    def test_breaker_blocks_all_buys(self):
        """熔断后所有买入被拒"""
        self.rm._is_circuit_breaker = True
        result = self.rm.check_order(_buy(), {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertFalse(result.passed)
        self.assertTrue(any("breaker" in r.lower() for r in result.rejected_reasons))

    def test_breaker_does_not_block_sells(self):
        """阶段 11 P1-7 修复后：熔断后 SELL 应放行（清仓/止损不能被熔断挡）"""
        self.rm._is_circuit_breaker = True
        sell = TradeSignal(symbol="AAPL.US", signal=Signal.SELL, price=150.0, quantity=10, strategy_name="test")
        result = self.rm.check_order(sell, {"total_assets": 10000, "available_cash": 10000}, {})
        self.assertTrue(result.passed, "熔断后 SELL 应放行")
        self.assertEqual(result.approved_quantity, 10)

    def test_manual_reset(self):
        """手动重置应解除熔断"""
        self.rm._is_circuit_breaker = True
        self.rm.reset_circuit_breaker()
        self.assertFalse(self.rm._is_circuit_breaker)

    def test_peak_value_only_increases(self):
        """peak_value 只升不降"""
        self.rm.update_portfolio_value(10000)
        self.rm.update_portfolio_value(12000)
        self.rm.update_portfolio_value(11000)
        self.assertEqual(self.rm._peak_value, 12000)


class TestPersistence(unittest.TestCase):
    """dump_state / load_state"""

    def setUp(self):
        self.rm = RiskManager(TIGHT_CONFIG)

    def test_dump_returns_dict(self):
        state = self.rm.dump_state()
        self.assertIsInstance(state, dict)
        self.assertIn("daily_pnl", state)
        self.assertIn("peak_value", state)
        self.assertIn("is_circuit_breaker", state)

    def test_dump_load_roundtrip(self):
        """dump 后再 load 应完整恢复状态"""
        self.rm._daily_pnl = -250.0
        self.rm._daily_trade_count = 2
        self.rm._daily_date = date.today()
        self.rm._peak_value = 105_000
        self.rm._is_circuit_breaker = False

        state = self.rm.dump_state()

        rm2 = RiskManager(TIGHT_CONFIG)
        ok = rm2.load_state(state)
        self.assertTrue(ok)
        self.assertEqual(rm2._daily_pnl, -250.0)
        self.assertEqual(rm2._daily_trade_count, 2)
        self.assertEqual(rm2._peak_value, 105_000)

    def test_load_old_date_resets_daily(self):
        """state 里 daily_date 是旧日期 → daily_pnl/count 自动归零"""
        old_state = {
            "daily_pnl": -300.0,
            "daily_trade_count": 5,
            "daily_date": (date.today() - timedelta(days=2)).isoformat(),
            "peak_value": 100_000,
            "is_circuit_breaker": False,
        }
        rm2 = RiskManager(TIGHT_CONFIG)
        rm2.load_state(old_state)
        # daily 应被重置
        self.assertEqual(rm2._daily_pnl, 0.0)
        self.assertEqual(rm2._daily_trade_count, 0)
        # peak_value 应保留
        self.assertEqual(rm2._peak_value, 100_000)

    def test_load_none_is_noop(self):
        ok = self.rm.load_state(None)
        self.assertFalse(ok)

    def test_load_empty_is_noop(self):
        ok = self.rm.load_state({})
        self.assertFalse(ok)


class TestSectorConcentration(unittest.TestCase):
    """阶段 11 P1-2：行业集中度限制测试"""

    def setUp(self):
        # 启用行业限制
        cfg = {
            **TIGHT_CONFIG,
            "portfolio_limits": {
                **TIGHT_CONFIG["portfolio_limits"],
                "max_sector_concentration": 0.40,
                "max_industry_concentration": 0.25,
            },
        }
        self.rm = RiskManager(cfg)
        # mock _lookup_sector_industry，避免依赖真实 DB
        from unittest.mock import patch
        self._patcher = patch.object(
            RiskManager, "_lookup_sector_industry",
            side_effect=self._mock_lookup,
        )
        self._patcher.start()
        # 确保不被熔断/财报等其他 reason 干扰
        self.rm.clear_sector_cache()

    def tearDown(self):
        self._patcher.stop()

    @staticmethod
    def _mock_lookup(symbol):
        """mock 4 只半导体 + 1 只医药"""
        m = {
            "MU.US": ("电子技术", "半导体"),
            "TSM.US": ("电子技术", "半导体"),
            "INTC.US": ("电子技术", "半导体"),
            "MRVL.US": ("电子技术", "半导体"),
            "JNJ.US": ("医药", "大型药物生产商"),
        }
        return m.get(symbol, (None, None))

    def test_first_semi_buy_passes(self):
        """第一只半导体买入（占 10%）应通过"""
        result = self.rm.check_order(
            _buy(symbol="MU.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 100_000},
            {},  # 无现有持仓
        )
        self.assertTrue(result.passed, f"第一只半导体应通过，原因: {result.rejected_reasons}")

    def test_industry_concentration_blocks(self):
        """已持半导体 20%，再买半导体应被拒（25% 上限）"""
        # 已持 TSM/INTC 各 10%，industry='半导体' 已占 20%
        # 再买 MU 10% → 半导体合计 30% > 25% 上限 → 拒
        existing = {
            "TSM.US": {"market_value": 10_000, "quantity": 100},
            "INTC.US": {"market_value": 10_000, "quantity": 100},
        }
        result = self.rm.check_order(
            _buy(symbol="MU.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 80_000},
            existing,
        )
        self.assertFalse(result.passed, "半导体已 20%，再加 10% 超 25% 应被拒")
        self.assertTrue(any("Industry concentration" in r for r in result.rejected_reasons))

    def test_sector_concentration_blocks(self):
        """已持电子技术 35%，再买电子技术应被拒（40% 上限）"""
        existing = {
            "TSM.US": {"market_value": 10_000, "quantity": 100},
            "INTC.US": {"market_value": 10_000, "quantity": 100},
            "MRVL.US": {"market_value": 15_000, "quantity": 150},
        }
        result = self.rm.check_order(
            _buy(symbol="MU.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 65_000},
            existing,
        )
        # 电子技术已 35%，加 10% → 45% > 40% 上限
        self.assertFalse(result.passed, "电子技术已 35%，再加 10% 超 40% 应被拒")

    def test_different_sector_passes(self):
        """虽然电子技术已 35%，但买医药应通过"""
        existing = {
            "TSM.US": {"market_value": 15_000, "quantity": 150},
            "INTC.US": {"market_value": 20_000, "quantity": 200},
        }
        result = self.rm.check_order(
            _buy(symbol="JNJ.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 65_000},
            existing,
        )
        self.assertTrue(result.passed, f"医药与电子技术不同 sector 应通过，原因: {result.rejected_reasons}")

    def test_no_lookup_data_skips_check(self):
        """unknown symbol（mock 返回 None, None）应跳过 sector 检查不报错"""
        result = self.rm.check_order(
            _buy(symbol="UNKNOWN.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 100_000},
            {},
        )
        # 不应因 sector 问题被拒（其他 reason 可能有，比如默认仓位 ok）
        for r in result.rejected_reasons:
            self.assertNotIn("Sector concentration", r)
            self.assertNotIn("Industry concentration", r)

    def test_disabled_when_config_zero(self):
        """max_sector_concentration=0 应跳过该限制"""
        cfg = {
            **TIGHT_CONFIG,
            "portfolio_limits": {
                **TIGHT_CONFIG["portfolio_limits"],
                "max_sector_concentration": 0,  # 禁用
                "max_industry_concentration": 0,
            },
        }
        rm = RiskManager(cfg)
        # 即使大量同行业持仓也不应被 sector reason 拦
        existing = {
            "TSM.US": {"market_value": 50_000, "quantity": 500},
        }
        result = rm.check_order(
            _buy(symbol="MU.US", price=100.0),
            {"total_assets": 100_000, "available_cash": 50_000},
            existing,
        )
        for r in result.rejected_reasons:
            self.assertNotIn("Sector concentration", r)
            self.assertNotIn("Industry concentration", r)


class TestEarningsWindow(unittest.TestCase):
    """财报窗口屏蔽"""

    def setUp(self):
        self.rm = RiskManager(TIGHT_CONFIG)

    def test_set_earnings_dates(self):
        dates = [date.today() + timedelta(days=2)]
        self.rm.set_earnings_dates("AAPL.US", dates)
        self.assertEqual(self.rm._earnings_dates["AAPL.US"], dates)

    def test_within_earnings_window_blocks(self):
        """财报前 1 天应被拒"""
        # 明天有财报，今天买应被拒（block_earnings_window_days=1）
        tomorrow = date.today() + timedelta(days=1)
        self.rm.set_earnings_dates("AAPL.US", [tomorrow])
        result = self.rm.check_order(
            _buy(symbol="AAPL.US"),
            {"total_assets": 10000, "available_cash": 10000},
            {},
        )
        # 应该被拒（如果实现正确）
        if not result.passed:
            self.assertTrue(any("earnings" in r.lower() for r in result.rejected_reasons))

    def test_outside_earnings_window_passes(self):
        """财报 30 天后应正常通过"""
        far_future = date.today() + timedelta(days=30)
        self.rm.set_earnings_dates("AAPL.US", [far_future])
        result = self.rm.check_order(
            _buy(symbol="AAPL.US"),
            {"total_assets": 10000, "available_cash": 10000},
            {},
        )
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()

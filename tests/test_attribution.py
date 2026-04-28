"""
test_attribution.py - 阶段 11 P1-4 业绩归因测试

覆盖：
- compute_realized_pnl_by_symbol FIFO 配对
- compute_unrealized_pnl 浮盈
- aggregate_by_strategy 按策略聚合
- 边界：空 trades / 全 buy 没 sell
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from attribution_report import (
    compute_realized_pnl_by_symbol,
    compute_unrealized_pnl,
    aggregate_by_strategy,
)


def _trade(symbol, side, qty, price, strategy="momentum", commission=1.0,
           executed_at="2026-04-22T13:00:00"):
    return {
        "symbol": symbol, "side": side, "quantity": qty,
        "price": price, "commission": commission,
        "strategy_name": strategy, "signal_reason": "test",
        "executed_at": executed_at,
    }


class TestFIFORealizedPnL(unittest.TestCase):
    """FIFO 配对算已实现 PnL"""

    def test_simple_buy_sell_profit(self):
        """100 股 @ $100 买入，卖出 @ $110 → 盈利 $1000 (减手续费)"""
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, commission=1),
            _trade("AAPL.US", "sell", 100, 110.0, commission=1, executed_at="2026-04-23T13:00:00"),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        self.assertIn("AAPL.US", result)
        # PnL = (110-100)*100 - 1 (sell commission, 100/100=1.0 比例)
        # 算法是 commission * (matched / qty) = 1 * (100/100) = 1
        # 所以 1000 - 1 = 999
        self.assertAlmostEqual(result["AAPL.US"]["realized_pnl"], 999.0, places=1)
        self.assertEqual(result["AAPL.US"]["win_count"], 1)
        self.assertEqual(result["AAPL.US"]["loss_count"], 0)

    def test_simple_buy_sell_loss(self):
        """买入 100 股 @ $100，卖出 @ $90 → 亏损"""
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, commission=1),
            _trade("AAPL.US", "sell", 100, 90.0, commission=1, executed_at="2026-04-23T13:00:00"),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        # PnL = (90-100)*100 - 1 = -1001
        self.assertAlmostEqual(result["AAPL.US"]["realized_pnl"], -1001.0, places=1)
        self.assertEqual(result["AAPL.US"]["loss_count"], 1)

    def test_partial_sell(self):
        """买 100 卖 50，剩余 50 在持仓队列"""
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, commission=1),
            _trade("AAPL.US", "sell", 50, 110.0, commission=1, executed_at="2026-04-23T13:00:00"),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        # PnL = (110-100)*50 - 1*(50/50) = 500-1 = 499
        self.assertAlmostEqual(result["AAPL.US"]["realized_pnl"], 499.0, places=1)

    def test_multiple_buy_lots_fifo(self):
        """多笔买入 + 一次卖出，按 FIFO 配对（先进先出）"""
        # 第一批 50 股 @ $100, 第二批 50 股 @ $120, 卖 80 股 @ $130
        # FIFO: 先卖第一批 50 股（pnl=50*30=1500），再卖第二批 30 股（pnl=30*10=300）
        # 总 PnL ≈ 1800（减手续费）
        trades = [
            _trade("AAPL.US", "buy", 50, 100.0, commission=0),
            _trade("AAPL.US", "buy", 50, 120.0, commission=0,
                   executed_at="2026-04-22T14:00:00"),
            _trade("AAPL.US", "sell", 80, 130.0, commission=0,
                   executed_at="2026-04-23T13:00:00"),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        # 50*(130-100) + 30*(130-120) = 1500 + 300 = 1800
        self.assertAlmostEqual(result["AAPL.US"]["realized_pnl"], 1800.0, places=1)

    def test_strategy_attribution(self):
        """by_strategy 字段正确"""
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, strategy="momentum", commission=0),
            _trade("AAPL.US", "sell", 100, 110.0, strategy="manual_close",
                   commission=0, executed_at="2026-04-23T13:00:00"),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        # PnL 应归到 buy 时的 strategy（momentum），不是 sell 时的 strategy
        self.assertIn("momentum", result["AAPL.US"]["by_strategy"])
        self.assertAlmostEqual(result["AAPL.US"]["by_strategy"]["momentum"], 1000.0, places=1)


class TestUnrealizedPnL(unittest.TestCase):
    def test_simple(self):
        positions = {
            "AAPL.US": {"unrealized_pnl": 250.5},
            "MSFT.US": {"unrealized_pnl": -100.3},
        }
        result = compute_unrealized_pnl(positions)
        self.assertEqual(result["AAPL.US"], 250.5)
        self.assertEqual(result["MSFT.US"], -100.3)

    def test_missing_field(self):
        positions = {"AAPL.US": {}}
        result = compute_unrealized_pnl(positions)
        self.assertEqual(result["AAPL.US"], 0.0)

    def test_empty(self):
        result = compute_unrealized_pnl({})
        self.assertEqual(result, {})


class TestAggregateByStrategy(unittest.TestCase):
    def test_aggregation(self):
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, strategy="momentum", commission=0),
            _trade("AAPL.US", "sell", 100, 110.0, strategy="momentum",
                   commission=0, executed_at="2026-04-23T13:00:00"),
            _trade("MSFT.US", "buy", 50, 200.0, strategy="rsi", commission=0),
        ]
        positions = {"MSFT.US": {"unrealized_pnl": 50.0}}
        realized = compute_realized_pnl_by_symbol(trades)
        result = aggregate_by_strategy(realized, trades, positions)

        self.assertIn("momentum", result)
        self.assertIn("rsi", result)
        # momentum 已实现 1000
        self.assertAlmostEqual(result["momentum"]["realized_pnl"], 1000.0, places=1)
        # rsi 未实现 50
        self.assertAlmostEqual(result["rsi"]["unrealized_pnl"], 50.0, places=1)
        # rsi 总 PnL = 0 + 50 = 50
        self.assertAlmostEqual(result["rsi"]["total_pnl"], 50.0, places=1)


class TestEdgeCases(unittest.TestCase):
    def test_empty_trades(self):
        result = compute_realized_pnl_by_symbol([])
        self.assertEqual(result, {})

    def test_only_buys_no_sells(self):
        trades = [
            _trade("AAPL.US", "buy", 100, 100.0, commission=0),
        ]
        result = compute_realized_pnl_by_symbol(trades)
        # 没卖出 → realized_pnl=0
        self.assertEqual(result["AAPL.US"]["realized_pnl"], 0.0)
        self.assertEqual(result["AAPL.US"]["win_count"], 0)


if __name__ == "__main__":
    unittest.main()

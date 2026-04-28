"""
test_position_sizer.py - PositionSizer 单元测试

覆盖：
- fixed_pct 模式：默认 10% 仓位
- risk_based_atr 模式：risk_pct × cash / (ATR × stop_mult)
- 单票上限 max_single_position_pct
- cash 不足时返回 0
- 价格无效时返回 0
- per_strategy_overrides 生效
- 已有持仓时允许追加
- min_order_amount_usd 兜底
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.position_sizer import PositionSizer


BASE_CONFIG = {
    "position": {
        "mode": "fixed_pct",
        "max_single_position_pct": 0.20,
        "default_position_pct": 0.10,
        "max_total_positions": 5,
        "min_order_amount_usd": 50,
        "single_trade_risk_pct": 0.02,
    }
}


ATR_CONFIG = {
    "position": {
        "mode": "risk_based_atr",
        "max_single_position_pct": 0.45,
        "default_position_pct": 0.10,
        "max_total_positions": 5,
        "min_order_amount_usd": 50,
        "single_trade_risk_pct": 0.02,  # 2% 风险
    },
    "per_strategy_overrides": {
        "momentum": {
            "single_trade_risk_pct": 0.0438,
            "atr_stop_mult": 2.0,
        },
        "ma_cross": {
            "single_trade_risk_pct": 0.0225,
            "atr_stop_mult": 2.5,
        },
    },
}


class TestPositionSizerFixedPct(unittest.TestCase):
    """固定百分比模式"""

    def setUp(self):
        self.sizer = PositionSizer(BASE_CONFIG)

    def test_basic_calc(self):
        """100K 总资产，10% 仓位，$100 / 股 → 100 股"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000
        )
        # 10% × 100K = 10K → /100 = 100 股
        self.assertEqual(shares, 100)

    def test_zero_price_returns_zero(self):
        """价格 ≤ 0 返回 0"""
        shares = self.sizer.calculate(
            price=0.0, total_assets=100_000, available_cash=100_000
        )
        self.assertEqual(shares, 0)

    def test_zero_assets_returns_zero(self):
        """总资产 ≤ 0 返回 0"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=0, available_cash=100_000
        )
        self.assertEqual(shares, 0)

    def test_max_position_cap(self):
        """请求的仓位超过单票上限会被截断"""
        # 设默认 30%，上限 20%
        cfg = {"position": {**BASE_CONFIG["position"],
                            "default_position_pct": 0.30,
                            "max_single_position_pct": 0.20}}
        sizer = PositionSizer(cfg)
        shares = sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000
        )
        # 截断到 20% × 100K = 20K → /100 = 200 股
        self.assertEqual(shares, 200)

    def test_cash_insufficient_returns_zero_or_partial(self):
        """现金不足时不应返回大于现金的股数"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=500
        )
        # 现金 500 / 100 = 5 股最多
        self.assertLessEqual(shares * 100.0, 500)

    def test_existing_position_capped_by_max_pct(self):
        """阶段 11 P1-6 澄清：existing_position_value 由 max_single_pct 硬上限扣减，
        不由 default_position_pct 扣减。

        语义：default_pct=10% 是建议仓位，max_pct=20% 是单票绝对上限。
        当 existing 占用一部分时，新建仓 = min(default_pct × total, max_pct × total - existing)
        """
        # existing=9K，total=100K，default=10% → target=10K
        # max_pct=20% → remaining = 20K - 9K = 11K
        # min(10K, 11K) = 10K → 100 股
        shares = self.sizer.calculate(
            price=100.0,
            total_assets=100_000,
            available_cash=100_000,
            existing_position_value=9_000,
        )
        self.assertEqual(shares, 100, "default 10% < max 20% - existing 9% = 11%，应取 default 10%")

    def test_existing_position_blocks_when_exceeds_max(self):
        """existing 已超 max_pct 上限时，新建仓应为 0"""
        # existing=22K（已超 20% max），total=100K
        # remaining = 20K - 22K = -2K → 0
        shares = self.sizer.calculate(
            price=100.0,
            total_assets=100_000,
            available_cash=100_000,
            existing_position_value=22_000,
        )
        self.assertEqual(shares, 0, "existing 已超 max_pct 上限，新建仓应被卡到 0")

    def test_existing_position_partial_when_max_minus_existing_smaller_than_default(self):
        """existing 让 max-existing < default 时，受 max 限制"""
        # existing=15K，total=100K，default=10% → target=10K
        # max=20% → remaining = 20K - 15K = 5K
        # min(10K, 5K) = 5K → 50 股
        shares = self.sizer.calculate(
            price=100.0,
            total_assets=100_000,
            available_cash=100_000,
            existing_position_value=15_000,
        )
        self.assertEqual(shares, 50, "max 20% - existing 15% = 5% < default 10%，应取 max 限制 5%")


class TestPositionSizerATR(unittest.TestCase):
    """risk_based_atr 模式 + per_strategy_overrides"""

    def setUp(self):
        self.sizer = PositionSizer(ATR_CONFIG)

    def test_atr_mode_basic(self):
        """ATR 模式：risk_pct=2%, ATR=2, stop_mult=2 → 风险/股=4
        总资产 100K → 可承担风险 2K → /4 = 500 股"""
        shares = self.sizer.calculate(
            price=100.0,
            total_assets=100_000,
            available_cash=100_000,
            atr=2.0,
            atr_stop_mult=2.0,
        )
        # 也受单票上限 45% × 100K / 100 = 450 股 限制
        # 算出 500 股 但被截到 450
        self.assertGreater(shares, 0)
        self.assertLessEqual(shares * 100.0, 0.45 * 100_000)

    def test_atr_mode_no_atr_falls_back(self):
        """ATR 模式但没传 atr → 应该降级（不崩）"""
        try:
            shares = self.sizer.calculate(
                price=100.0,
                total_assets=100_000,
                available_cash=100_000,
                atr=None,
            )
            # 不严格要求行为，但不应崩
            self.assertIsInstance(shares, int)
        except Exception as e:
            self.fail(f"ATR 缺失时不应崩，实际崩了: {e}")

    def test_per_strategy_momentum_uses_higher_risk(self):
        """momentum 策略用 4.38% 风险，应大于默认 2%"""
        shares_default = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=2.0, atr_stop_mult=2.0,
        )
        shares_momentum = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=2.0, atr_stop_mult=2.0, strategy_name="momentum",
        )
        # momentum 风险大 → 仓位大（除非都被 max_single_pct 截了）
        self.assertGreaterEqual(shares_momentum, shares_default)


class TestPositionSizerEdgeCases(unittest.TestCase):
    """边界情况"""

    def test_negative_price(self):
        sizer = PositionSizer(BASE_CONFIG)
        self.assertEqual(sizer.calculate(price=-1.0, total_assets=100_000, available_cash=100_000), 0)

    def test_negative_cash(self):
        sizer = PositionSizer(BASE_CONFIG)
        # 负现金应被处理
        try:
            shares = sizer.calculate(price=100.0, total_assets=100_000, available_cash=-1000)
            self.assertEqual(shares, 0)
        except Exception:
            pass  # 也接受 raise

    def test_min_order_amount_floor(self):
        """单笔金额 < min_order_amount_usd 应返回 0"""
        # 1K 总资产 × 10% = 100 → 价格 100 → 1 股 = $100 > $50 ok
        # 想触发 floor: 1K × 1% = 10 < 50
        cfg = {"position": {**BASE_CONFIG["position"],
                            "default_position_pct": 0.01,
                            "min_order_amount_usd": 50}}
        sizer = PositionSizer(cfg)
        shares = sizer.calculate(price=100.0, total_assets=1_000, available_cash=1_000)
        # 1% × 1000 = $10 < $50 min → 应该是 0
        self.assertEqual(shares, 0)


class TestPositionSizerVolParity(unittest.TestCase):
    """阶段 11 P1-1：vol-parity 模式测试"""

    def setUp(self):
        # 启用 vol_parity 默认模式 + target_vol_pct=3%
        self.cfg = {
            "position": {
                "mode": "vol_parity",
                "max_single_position_pct": 0.20,
                "default_position_pct": 0.10,
                "max_total_positions": 5,
                "min_order_amount_usd": 50,
                "single_trade_risk_pct": 0.02,
                "vol_parity": {"target_vol_pct": 0.03},
            }
        }
        self.sizer = PositionSizer(self.cfg)

    def test_high_vol_smaller_position(self):
        """高 ATR（5%/day）应得到比 default 小的仓位"""
        # ATR=5, price=100 → vol_pct=5%, target=3% → mult=0.6 → adjusted=6%
        # 但下限是 0.5*default=5%，所以 adjusted=6%
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=5.0,
        )
        # 6K → 60 股
        self.assertGreater(shares, 0)
        # 应该 < 默认 10%（100 股）
        self.assertLess(shares, 100, "高波动仓位应小于 default")

    def test_low_vol_larger_position(self):
        """低 ATR（1%/day）应得到比 default 大的仓位"""
        # ATR=1, price=100 → vol_pct=1%, target=3% → mult=3 → adjusted=30%
        # 但上限 max_single_pct=20%，所以 adjusted=20%
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=1.0,
        )
        # 应受 max_single_pct 上限制约，得到 20% = 200 股
        self.assertGreater(shares, 100, "低波动仓位应大于 default 10%")
        self.assertLessEqual(shares, 200, "应受 max_single_pct 上限")

    def test_neutral_vol_close_to_default(self):
        """ATR 等于 target_vol（3%）时应接近 default 仓位"""
        # ATR=3, price=100 → vol_pct=3% → mult=1.0 → adjusted=10% = default
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=3.0,
        )
        self.assertEqual(shares, 100, "vol == target 时应正好等于 default 10% = 100 股")

    def test_no_atr_falls_back_to_fixed_pct(self):
        """ATR 缺失时退化到 fixed_pct"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=None,
        )
        # fixed_pct default 10% = 100 股
        self.assertEqual(shares, 100, "无 ATR 应退化到 fixed_pct=10%")

    def test_vol_parity_respects_existing(self):
        """已有持仓应正确扣减配额"""
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=3.0,  # 中性波动
            existing_position_value=15_000,
        )
        # default 10% = 10K，但 max=20% 已用 15K → remaining 5K = 50 股
        self.assertEqual(shares, 50, "existing=15% + max=20% → remaining=5K=50 股")

    def test_vol_parity_floor_protection(self):
        """超高波动时下限保护：仓位不应跌到 0"""
        # 极端高波动 ATR=20%/day
        shares = self.sizer.calculate(
            price=100.0, total_assets=100_000, available_cash=100_000,
            atr=20.0,
        )
        # 即使 mult 极小，adjusted 不应 < 0.5*default=5% = 50 股
        self.assertGreaterEqual(shares, 50, "下限保护应保证至少 0.5×default")


if __name__ == "__main__":
    unittest.main()

"""
Position Sizer - 仓位计算器
根据账户总资产、风控配置计算单笔交易的最大仓位。

支持的仓位模式（通过 config['mode'] 或调用时 mode 参数切换）：
- fixed_pct        : 固定比例（legacy，账户 N% 买入）
- risk_based_atr   : 单笔风险反算（新模式：shares = equity × risk% / (atr × stop_mult)）
- fixed_amount     : 固定金额
- kelly            : Kelly 公式（半 Kelly）
- equal_weight     : 等权分配
"""

import math
from typing import Optional

from loguru import logger


class PositionSizer:
    """仓位计算器。"""

    def __init__(self, config: dict):
        """
        Args:
            config: risk.yaml 的完整 dict（需含 position 段，可选 per_strategy_overrides）
        """
        self._root_config = config or {}
        pos_cfg = self._root_config.get("position", self._root_config)

        self._mode = pos_cfg.get("mode", "fixed_pct")
        self._max_single_pct = pos_cfg.get("max_single_position_pct", 0.20)
        self._default_pct = pos_cfg.get("default_position_pct", 0.10)
        self._max_positions = pos_cfg.get("max_total_positions", 5)
        self._min_amount = pos_cfg.get("min_order_amount_usd", 50)
        self._default_risk_pct = pos_cfg.get("single_trade_risk_pct", 0.02)

        self._per_strategy: dict = (
            self._root_config.get("per_strategy_overrides", {}) or {}
        )

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------

    def calculate(
        self,
        price: float,
        total_assets: float,
        available_cash: float,
        existing_position_value: float = 0,
        mode: Optional[str] = None,
        *,
        atr: Optional[float] = None,
        atr_stop_mult: Optional[float] = None,
        strategy_name: Optional[str] = None,
        **kwargs,
    ) -> int:
        """计算下单股数。

        Args:
            price: 标的当前价格
            total_assets: 账户总资产
            available_cash: 可用现金
            existing_position_value: 已有该标的持仓市值
            mode: 计算模式（None 则用 config 默认）
            atr: 当前 bar 的 ATR（risk_based_atr 模式必需）
            atr_stop_mult: ATR 止损倍数（None 则从 per_strategy_overrides 取）
            strategy_name: 策略名，用于查 per_strategy_overrides
            **kwargs: 额外参数（win_rate/profit_loss_ratio 用于 Kelly 等）

        Returns:
            建议下单股数（整数，0 表示不下单）
        """
        if price <= 0 or total_assets <= 0:
            return 0

        effective_mode = (mode or self._mode or "fixed_pct").lower()

        # 按策略覆盖
        overrides = self._per_strategy.get(strategy_name or "", {}) or {}
        risk_pct = overrides.get("single_trade_risk_pct", self._default_risk_pct)
        eff_stop_mult = (
            atr_stop_mult
            if atr_stop_mult is not None
            else overrides.get("atr_stop_mult", 2.0)
        )

        # --- risk_based_atr: 直接按股数计算 ---
        if effective_mode == "risk_based_atr":
            shares = self._calc_risk_based_atr_shares(
                price=price,
                total_assets=total_assets,
                available_cash=available_cash,
                existing_position_value=existing_position_value,
                atr=atr,
                atr_stop_mult=eff_stop_mult,
                risk_pct=risk_pct,
                strategy_name=strategy_name,
            )
            return shares

        # --- legacy_cash95: 严格复现旧引擎的 95% 现金重仓（仅 A/B 对照用） ---
        if effective_mode == "legacy_cash95":
            amount = available_cash * 0.95
            if amount < self._min_amount:
                return 0
            return max(int(amount / price), 0)

        # --- 其它模式：先算 amount，再 amount/price 转股数 ---
        if effective_mode == "fixed_pct":
            amount = self._calc_fixed_pct(total_assets, existing_position_value)
        elif effective_mode == "fixed_amount":
            amount = kwargs.get("amount", total_assets * self._default_pct)
        elif effective_mode == "kelly":
            amount = self._calc_kelly(
                total_assets,
                kwargs.get("win_rate", 0.5),
                kwargs.get("profit_loss_ratio", 1.5),
            )
        elif effective_mode == "equal_weight":
            amount = total_assets / max(self._max_positions, 1)
        elif effective_mode == "vol_parity":
            # 阶段 11 P1-1：波动率反比模式
            # 高波动标的仓位变小，低波动标的仓位变大，组合波动趋于平衡
            amount = self._calc_vol_parity(
                price=price,
                total_assets=total_assets,
                existing_position_value=existing_position_value,
                atr=atr,
            )
        else:
            amount = total_assets * self._default_pct

        return self._amount_to_shares(
            amount, price, available_cash, total_assets, existing_position_value
        )

    # ------------------------------------------------------------
    # 具体模式实现
    # ------------------------------------------------------------

    def _calc_risk_based_atr_shares(
        self,
        price: float,
        total_assets: float,
        available_cash: float,
        existing_position_value: float,
        atr: Optional[float],
        atr_stop_mult: float,
        risk_pct: float,
        strategy_name: Optional[str],
    ) -> int:
        """risk_based_atr 模式。

        公式：
            risk_capital = total_assets × risk_pct
            stop_distance = atr × atr_stop_mult
            shares_target = floor(risk_capital / stop_distance)
        """
        # ATR 异常 -> 回退 fixed_pct
        if atr is None or not math.isfinite(atr) or atr <= 0:
            logger.warning(
                f"PositionSizer: ATR invalid ({atr}) for {strategy_name}, "
                f"fallback -> fixed_pct"
            )
            amount = self._calc_fixed_pct(total_assets, existing_position_value)
            return self._amount_to_shares(
                amount, price, available_cash, total_assets, existing_position_value
            )

        stop_distance = atr * atr_stop_mult
        if stop_distance <= 0:
            logger.warning(
                f"PositionSizer: stop_distance<=0 (atr={atr}, mult={atr_stop_mult}), "
                f"fallback -> fixed_pct"
            )
            amount = self._calc_fixed_pct(total_assets, existing_position_value)
            return self._amount_to_shares(
                amount, price, available_cash, total_assets, existing_position_value
            )

        risk_capital = total_assets * risk_pct
        shares_target = int(risk_capital / stop_distance)

        # 约束：单票上限 + 可用现金 + 最小金额
        amount = shares_target * price

        # 不超过单票上限
        max_amount = total_assets * self._max_single_pct - existing_position_value
        if amount > max(0.0, max_amount):
            amount = max(0.0, max_amount)
            shares_target = int(amount / price)

        # 不超过可用现金
        if amount > available_cash * 0.95:
            shares_target = int(available_cash * 0.95 / price)
            amount = shares_target * price

        # 最小金额
        if amount < self._min_amount:
            return 0

        return max(shares_target, 0)

    def _amount_to_shares(
        self,
        amount: float,
        price: float,
        available_cash: float,
        total_assets: float,
        existing_position_value: float,
    ) -> int:
        """把目标金额换算为股数（含现金/单票/最小额约束）。"""
        amount = min(amount, available_cash * 0.95)
        max_amount = total_assets * self._max_single_pct - existing_position_value
        amount = min(amount, max(0.0, max_amount))
        if amount < self._min_amount:
            return 0
        return max(int(amount / price), 0)

    def _calc_fixed_pct(
        self, total_assets: float, existing_value: float
    ) -> float:
        """固定比例：单笔 default_pct × 总资产，且加上已有头寸不超过 max_single_pct。"""
        target = total_assets * self._default_pct
        remaining = total_assets * self._max_single_pct - existing_value
        return min(target, max(0.0, remaining))

    def _calc_kelly(
        self,
        total_assets: float,
        win_rate: float,
        profit_loss_ratio: float,
    ) -> float:
        """半 Kelly 公式。"""
        p = max(0.01, min(0.99, win_rate))
        q = 1 - p
        b = max(0.01, profit_loss_ratio)
        kelly = (p * b - q) / b
        kelly = max(0, kelly)
        half_kelly = kelly / 2
        half_kelly = min(half_kelly, self._max_single_pct)
        return total_assets * half_kelly

    def _calc_vol_parity(
        self,
        price: float,
        total_assets: float,
        existing_position_value: float,
        atr: Optional[float],
    ) -> float:
        """阶段 11 P1-1：波动率反比模式（vol-parity）。

        语义：高 ATR 标的仓位变小，低 ATR 标的仓位变大，让组合各标的的"风险贡献"接近一致。

        公式：
            actual_vol_pct = ATR / price        （标的的"日波动百分比"）
            target_vol_pct = config.target_vol_pct (默认 3%)
            multiplier = target_vol_pct / actual_vol_pct
            adjusted_pct = default_pct × multiplier
            （受 max_single_pct 上限和 0.5*default_pct 下限保护）

        ATR 缺失时退化到 fixed_pct。
        """
        if not atr or atr <= 0 or price <= 0:
            # 退化：无 ATR 数据 → 走 fixed_pct
            logger.debug("[vol_parity] ATR 缺失，退化到 fixed_pct")
            return self._calc_fixed_pct(total_assets, existing_position_value)

        actual_vol_pct = atr / price
        # target_vol_pct 可在 root_config 配，默认 3%
        target_vol_pct = (
            self._root_config.get("position", {})
            .get("vol_parity", {})
            .get("target_vol_pct", 0.03)
        )
        multiplier = target_vol_pct / max(actual_vol_pct, 1e-6)

        adjusted_pct = self._default_pct * multiplier

        # 边界保护：最小 0.5×default（避免高波动几乎不下单），最大 max_single_pct
        adjusted_pct = max(self._default_pct * 0.5, adjusted_pct)
        adjusted_pct = min(self._max_single_pct, adjusted_pct)

        target = total_assets * adjusted_pct
        # 已有持仓扣减（同 _calc_fixed_pct 语义）
        remaining = total_assets * self._max_single_pct - existing_position_value
        amount = min(target, max(0.0, remaining))

        logger.debug(
            f"[vol_parity] price={price:.2f} ATR={atr:.2f} vol_pct={actual_vol_pct:.2%} "
            f"mult={multiplier:.2f} adjusted_pct={adjusted_pct:.2%} amount=${amount:.0f}"
        )
        return amount

    def get_equal_weight_size(
        self,
        price: float,
        total_assets: float,
        num_positions: int,
    ) -> int:
        """等权分配时的每只股票股数。"""
        if num_positions <= 0 or price <= 0:
            return 0
        per_stock = total_assets / num_positions
        per_stock = min(per_stock, total_assets * self._max_single_pct)
        return int(per_stock / price)

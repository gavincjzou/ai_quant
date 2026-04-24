"""
MA Cross Strategy - 均线交叉策略
短期均线上穿长期均线买入，下穿卖出。

阶段6 升级：金叉增加成交量确认（趋势+确认组合）。
"""

from typing import Optional

import pandas as pd
from loguru import logger

from src.strategy.base_strategy import BaseStrategy, Signal, TradeSignal


class MACrossStrategy(BaseStrategy):
    """
    均线交叉策略。

    逻辑:
    - 短期均线从下方上穿长期均线 -> BUY (金叉)
        * 阶段6 增强：如果 volume_confirm_enabled=True，
          还需要当日成交量 > 20日均量 × volume_multiplier 才发信号
    - 短期均线从上方下穿长期均线 -> SELL (死叉)
        * 死叉保持原逻辑（快速止损优先，不加量能门槛）
    - 否则 -> HOLD

    参数:
    - short_period: 短期均线周期 (默认5天)
    - long_period: 长期均线周期 (默认20天)
    - signal_type: SMA | EMA (默认SMA)
    - volume_confirm_enabled: 是否启用量能确认 (默认 True)
    - volume_period: 成交量均线周期 (默认20天)
    - volume_multiplier: 量能倍数阈值 (默认1.5)
    """

    def init(self, config: dict) -> None:
        self._name = "ma_cross"
        self._params = {
            "short_period": config.get("short_period", 5),
            "long_period": config.get("long_period", 20),
            "signal_type": config.get("signal_type", "SMA"),
            # --- 阶段6 新增：量能确认 ---
            "volume_confirm_enabled": config.get("volume_confirm_enabled", True),
            "volume_period": config.get("volume_period", 20),
            "volume_multiplier": config.get("volume_multiplier", 1.5),
        }
        logger.info(f"Strategy '{self.name}' initialized: {self._params}")

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> Optional[TradeSignal]:
        short_p = self._params["short_period"]
        long_p = self._params["long_period"]
        sig_type = self._params["signal_type"]
        vol_confirm = self._params["volume_confirm_enabled"]
        vol_period = self._params["volume_period"]
        vol_mult = self._params["volume_multiplier"]

        # 数据量不足
        if len(data) < long_p + 2:
            return None

        # 计算均线
        if sig_type == "EMA":
            data = data.copy()
            data["ma_short"] = data["close"].ewm(span=short_p, adjust=False).mean()
            data["ma_long"] = data["close"].ewm(span=long_p, adjust=False).mean()
        else:  # SMA
            data = data.copy()
            data["ma_short"] = data["close"].rolling(window=short_p).mean()
            data["ma_long"] = data["close"].rolling(window=long_p).mean()

        # 取最后两根 bar 判断交叉
        curr_short = data["ma_short"].iloc[-1]
        curr_long = data["ma_long"].iloc[-1]
        prev_short = data["ma_short"].iloc[-2]
        prev_long = data["ma_long"].iloc[-2]
        current_price = data["close"].iloc[-1]

        # ------------------------------------------------------------
        # 金叉：短均线从下方穿越长均线
        # ------------------------------------------------------------
        if prev_short <= prev_long and curr_short > curr_long:
            # 阶段6：量能确认层
            if vol_confirm:
                passed, vol_info = self._check_volume_confirm(data, vol_period, vol_mult)
                if not passed:
                    logger.debug(
                        f"{symbol} MA 金叉但量能不足: {vol_info}，过滤信号"
                    )
                    return None

            return TradeSignal(
                symbol=symbol,
                signal=Signal.BUY,
                price=current_price,
                reason=f"Golden cross: MA{short_p}({curr_short:.2f}) > MA{long_p}({curr_long:.2f})"
                       + (f" + 放量确认" if vol_confirm else ""),
                confidence=min(0.5 + abs(curr_short - curr_long) / current_price * 10, 1.0),
                strategy_name=self.name,
            )

        # ------------------------------------------------------------
        # 死叉：短均线从上方穿越长均线（保持原逻辑，不加量能门槛）
        # ------------------------------------------------------------
        if prev_short >= prev_long and curr_short < curr_long:
            return TradeSignal(
                symbol=symbol,
                signal=Signal.SELL,
                price=current_price,
                reason=f"Death cross: MA{short_p}({curr_short:.2f}) < MA{long_p}({curr_long:.2f})",
                confidence=min(0.5 + abs(curr_long - curr_short) / current_price * 10, 1.0),
                strategy_name=self.name,
            )

        return None  # HOLD

    # ----------------------------------------------------------------
    @staticmethod
    def _check_volume_confirm(
        data: pd.DataFrame,
        period: int,
        multiplier: float,
    ) -> tuple[bool, str]:
        """检查量能确认条件。

        返回 (是否通过, 日志信息字符串)。

        回退逻辑（任一条件则视为通过，避免数据缺失阻塞信号）：
        - DataFrame 没有 volume 列 -> 通过
        - 数据量 < period + 2 -> 通过
        - 成交量均线计算结果为 NaN -> 通过
        """
        if "volume" not in data.columns:
            return True, "no_volume_column"

        if len(data) < period + 2:
            return True, f"insufficient_data({len(data)}<{period+2})"

        vol_ma = data["volume"].rolling(window=period).mean().iloc[-1]
        curr_vol = data["volume"].iloc[-1]

        if pd.isna(vol_ma) or vol_ma <= 0:
            return True, f"vol_ma_invalid({vol_ma})"

        threshold = vol_ma * multiplier
        if curr_vol >= threshold:
            return True, f"vol={curr_vol:.0f} ≥ ma{period}×{multiplier}={threshold:.0f}"
        else:
            return False, f"vol={curr_vol:.0f} < ma{period}×{multiplier}={threshold:.0f}"

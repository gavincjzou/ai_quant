"""
Momentum Strategy - 动量策略
基于 N 日收益率选股，买入动量最强标的，卖出动量最弱标的。
"""

from typing import Optional

import pandas as pd
from loguru import logger

from src.strategy.base_strategy import BaseStrategy, Signal, TradeSignal


class MomentumStrategy(BaseStrategy):
    """
    动量策略。
    
    逻辑:
    - 计算标的 N 日收益率 (ROC)
    - 如果 ROC > 阈值 -> BUY (正向动量)
    - 如果 ROC < -阈值 -> SELL (负向动量/动量衰减)
    - 否则 -> HOLD
    
    注意: 在多标的场景下，通常由 StrategyManager 做跨标的排序。
    单标的场景下，仅判断该标的自身的动量方向。
    
    参数:
    - lookback_period: 回看周期 (默认20天)
    - buy_threshold: 买入阈值 (默认0.05, 即5%)
    - sell_threshold: 卖出阈值 (默认-0.03, 即-3%)
    """

    def init(self, config: dict) -> None:
        self._name = "momentum"
        self._params = {
            "lookback_period": config.get("lookback_period", 20),
            "buy_threshold": config.get("buy_threshold", 0.05),
            "sell_threshold": config.get("sell_threshold", -0.03),
            "top_n": config.get("top_n", 3),
            "rebalance_days": config.get("rebalance_days", 5),
        }
        self._state["bar_count"] = 0
        logger.info(f"Strategy '{self.name}' initialized: {self._params}")

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> Optional[TradeSignal]:
        lookback = self._params["lookback_period"]
        buy_thresh = self._params["buy_threshold"]
        sell_thresh = self._params["sell_threshold"]

        if len(data) < lookback + 1:
            return None

        # 计算 N 日收益率 (Rate of Change)
        current_price = data["close"].iloc[-1]
        past_price = data["close"].iloc[-lookback - 1]
        roc = (current_price - past_price) / past_price

        # 计算短期动量加速度（最近5日 vs 前5日）
        if len(data) >= lookback + 6:
            recent_roc = (data["close"].iloc[-1] - data["close"].iloc[-6]) / data["close"].iloc[-6]
            older_roc = (data["close"].iloc[-6] - data["close"].iloc[-11]) / data["close"].iloc[-11]
            acceleration = recent_roc - older_roc
        else:
            acceleration = 0

        # 正向动量 -> 买入
        if roc > buy_thresh:
            confidence = min(0.5 + roc * 2, 1.0)
            # 加速度为正进一步增加置信度
            if acceleration > 0:
                confidence = min(confidence + 0.1, 1.0)
            return TradeSignal(
                symbol=symbol,
                signal=Signal.BUY,
                price=current_price,
                reason=f"Momentum: {lookback}d ROC={roc:.2%}, accel={acceleration:.2%}",
                confidence=confidence,
                strategy_name=self.name,
            )

        # 负向动量 -> 卖出
        if roc < sell_thresh:
            confidence = min(0.5 + abs(roc) * 2, 1.0)
            return TradeSignal(
                symbol=symbol,
                signal=Signal.SELL,
                price=current_price,
                reason=f"Momentum reversal: {lookback}d ROC={roc:.2%}",
                confidence=confidence,
                strategy_name=self.name,
            )

        return None  # HOLD

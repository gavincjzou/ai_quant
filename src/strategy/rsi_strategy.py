"""
RSI Strategy - RSI 反转策略
RSI 低于超卖阈值买入，高于超买阈值卖出。

阶段6 升级：超卖买入增加 MA50 趋势过滤（均值回归+趋势组合）。
"""

from typing import Optional

import pandas as pd
from loguru import logger

from src.strategy.base_strategy import BaseStrategy, Signal, TradeSignal


class RSIStrategy(BaseStrategy):
    """
    RSI 反转策略。

    逻辑:
    - RSI < oversold (30) -> BUY (超卖反弹)
        * 阶段6 增强：如果 trend_filter_enabled=True，
          还需要当前价格 > MA{trend_ma_period} 才发信号
          （只在上升趋势中抄底，避免下跌中"越抄越跌"）
    - RSI > overbought (70) -> SELL (超买回调)
        * 超买卖出保持原逻辑（下跌趋势中的 RSI>70 也是卖出信号，不能被趋势过滤反向屏蔽）
    - 否则 -> HOLD

    参数:
    - period: RSI 周期 (默认14)
    - overbought: 超买阈值 (默认70)
    - oversold: 超卖阈值 (默认30)
    - trend_filter_enabled: 是否启用趋势过滤 (默认 True)
    - trend_ma_period: 趋势判断 MA 周期 (默认50)
    """

    def init(self, config: dict) -> None:
        self._name = "rsi"
        self._params = {
            "period": config.get("period", 14),
            "overbought": config.get("overbought", 70),
            "oversold": config.get("oversold", 30),
            # --- 阶段6 新增：趋势过滤 ---
            "trend_filter_enabled": config.get("trend_filter_enabled", True),
            "trend_ma_period": config.get("trend_ma_period", 50),
        }
        logger.info(f"Strategy '{self.name}' initialized: {self._params}")

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int) -> pd.Series:
        """计算 RSI 指标（Wilder 平滑，向量化 ewm 实现，性能 10x+）"""
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        # Wilder 平滑 α = 1/period，等价于原有 for 循环实现
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        # loss 全为 0 时 RSI 应为 100
        rsi = rsi.fillna(100)
        return rsi

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> Optional[TradeSignal]:
        period = self._params["period"]
        overbought = self._params["overbought"]
        oversold = self._params["oversold"]
        trend_filter = self._params["trend_filter_enabled"]
        trend_ma_p = self._params["trend_ma_period"]

        # 数据量不足
        if len(data) < period + 2:
            return None

        rsi = self._calc_rsi(data["close"], period)
        current_rsi = rsi.iloc[-1]
        current_price = data["close"].iloc[-1]

        if pd.isna(current_rsi):
            return None

        # ------------------------------------------------------------
        # 超卖区域 -> 买入信号
        # ------------------------------------------------------------
        if current_rsi < oversold:
            # 阶段6：趋势过滤层
            if trend_filter:
                passed, trend_info = self._check_trend_filter(
                    data, current_price, trend_ma_p
                )
                if not passed:
                    logger.debug(
                        f"{symbol} RSI 超卖但趋势向下: {trend_info}，过滤信号"
                    )
                    return None

            # 置信度：RSI 越低越强
            confidence = min(0.5 + (oversold - current_rsi) / oversold, 1.0)
            return TradeSignal(
                symbol=symbol,
                signal=Signal.BUY,
                price=current_price,
                reason=f"RSI oversold: {current_rsi:.1f} < {oversold}"
                       + (f" + 上升趋势" if trend_filter else ""),
                confidence=confidence,
                strategy_name=self.name,
            )

        # ------------------------------------------------------------
        # 超买区域 -> 卖出信号（保持原逻辑，不加趋势过滤）
        # ------------------------------------------------------------
        if current_rsi > overbought:
            confidence = min(0.5 + (current_rsi - overbought) / (100 - overbought), 1.0)
            return TradeSignal(
                symbol=symbol,
                signal=Signal.SELL,
                price=current_price,
                reason=f"RSI overbought: {current_rsi:.1f} > {overbought}",
                confidence=confidence,
                strategy_name=self.name,
            )

        return None  # HOLD

    # ----------------------------------------------------------------
    @staticmethod
    def _check_trend_filter(
        data: pd.DataFrame,
        current_price: float,
        trend_ma_period: int,
    ) -> tuple[bool, str]:
        """检查趋势过滤条件。

        返回 (是否通过, 日志信息字符串)。

        回退逻辑（任一条件则视为通过，避免数据缺失阻塞信号）：
        - 数据量 < trend_ma_period（正好等于 lookback 约束末尾的边界）-> 通过
        - MA 计算结果为 NaN -> 通过
        - current_price > MA{trend_ma_period} -> 通过（上升趋势）
        """
        if len(data) < trend_ma_period:
            return True, f"insufficient_data({len(data)}<{trend_ma_period})"

        ma_trend = data["close"].rolling(window=trend_ma_period).mean().iloc[-1]

        if pd.isna(ma_trend):
            return True, f"ma_trend_nan"

        if current_price > ma_trend:
            return True, f"price={current_price:.2f} > MA{trend_ma_period}={ma_trend:.2f}"
        else:
            return False, f"price={current_price:.2f} ≤ MA{trend_ma_period}={ma_trend:.2f}"

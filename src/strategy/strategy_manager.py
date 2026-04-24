"""
Strategy Manager - 策略管理器
根据配置加载、注册和运行策略，汇总各策略信号。
"""

from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from src.strategy.ma_cross_strategy import MACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy
from src.strategy.momentum_strategy import MomentumStrategy


# 策略注册表
STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
    "rsi": RSIStrategy,
    "momentum": MomentumStrategy,
}


class StrategyManager:
    """策略管理器：加载、运行、汇总多策略信号。

    阶段8 新增：支持 per_symbol_strategies 配置，每只标的只跑指定策略。
    例如在 strategies.yaml 中：
        per_symbol_strategies:
          AAPL.US: [rsi]
          NVDA.US: [momentum]
          AMD.US: [ma_cross]
    未在 per_symbol_strategies 中列出的标的走 active_strategies（默认行为）。
    """

    def __init__(self, config: dict):
        """
        Args:
            config: strategies.yaml 的完整配置字典
        """
        self._config = config
        self._strategies: Dict[str, BaseStrategy] = {}
        # 阶段8 新增：per_symbol 策略映射
        self._per_symbol: Dict[str, List[str]] = (
            config.get("per_symbol_strategies", {}) or {}
        )
        self._load_strategies()

    def _load_strategies(self):
        """根据配置加载启用的策略"""
        active = self._config.get("active_strategies", [])
        # 阶段8：即使 active 里没有但 per_symbol 里用到的策略，也要加载
        needed = set(active)
        for syms_strats in self._per_symbol.values():
            needed.update(syms_strats or [])

        for name in needed:
            if name not in STRATEGY_REGISTRY:
                logger.warning(f"Unknown strategy: '{name}', skipping")
                continue

            strategy_class = STRATEGY_REGISTRY[name]
            strategy = strategy_class()
            strategy_config = self._config.get(name, {})
            strategy.init(strategy_config)
            self._strategies[name] = strategy

        if self._per_symbol:
            logger.info(
                f"StrategyManager loaded {len(self._strategies)} strategies: "
                f"{list(self._strategies.keys())} | "
                f"per_symbol 映射: {len(self._per_symbol)} 只标的"
            )
        else:
            logger.info(
                f"StrategyManager loaded {len(self._strategies)} strategies: "
                f"{list(self._strategies.keys())}"
            )

    def _strategies_for_symbol(self, symbol: str) -> List[str]:
        """
        阶段8：返回该 symbol 应该跑的策略名列表。

        优先级：per_symbol_strategies[symbol] > active_strategies
        """
        if symbol in self._per_symbol:
            # 有显式配置（即使空列表也表示"禁用所有策略"）
            return self._per_symbol[symbol] or []
        # 回退到 active_strategies
        return self._config.get("active_strategies", []) or list(self._strategies.keys())

    def run_all(
        self,
        symbol: str,
        data: pd.DataFrame,
        allow_overlap: bool = False,
    ) -> List[TradeSignal]:
        """
        对一个标的运行对应的策略。

        阶段8 升级：根据 per_symbol_strategies 决定跑哪些策略。

        Args:
            symbol: 标的代码
            data: OHLCV DataFrame
            allow_overlap: 是否允许多策略信号叠加

        Returns:
            信号列表（可能为空）
        """
        signals: List[TradeSignal] = []
        # 阶段8：只跑该 symbol 应该跑的策略
        strategy_names = self._strategies_for_symbol(symbol)

        for name in strategy_names:
            strategy = self._strategies.get(name)
            if strategy is None:
                continue
            try:
                signal = strategy.generate_signal(symbol, data)
                if signal is not None:
                    signals.append(signal)
                    logger.debug(f"Signal from {name}: {signal}")
            except Exception as e:
                logger.error(f"Strategy '{name}' error on {symbol}: {e}")

        if not allow_overlap and len(signals) > 1:
            # 如果不允许叠加，选置信度最高的信号
            signals = [max(signals, key=lambda s: s.confidence)]
            logger.debug(f"Signal overlap resolved: kept {signals[0]}")

        return signals

    def run_watchlist(
        self,
        watchlist: List[str],
        data_map: Dict[str, pd.DataFrame],
    ) -> Dict[str, List[TradeSignal]]:
        """
        对整个观察列表运行策略。
        
        Args:
            watchlist: 标的代码列表
            data_map: {symbol: DataFrame} 映射
            
        Returns:
            {symbol: [signals]} 映射
        """
        allow_overlap = self._config.get("execution", {}).get(
            "allow_signal_overlap", False
        )

        result: Dict[str, List[TradeSignal]] = {}
        for symbol in watchlist:
            data = data_map.get(symbol)
            if data is None or data.empty:
                logger.warning(f"No data for {symbol}, skipping")
                continue

            signals = self.run_all(symbol, data, allow_overlap)
            if signals:
                result[symbol] = signals

        # 汇总日志
        buy_count = sum(
            1 for sigs in result.values() for s in sigs if s.signal == Signal.BUY
        )
        sell_count = sum(
            1 for sigs in result.values() for s in sigs if s.signal == Signal.SELL
        )
        logger.info(
            f"Watchlist scan complete: {len(watchlist)} symbols, "
            f"{buy_count} BUY signals, {sell_count} SELL signals"
        )

        return result

    def get_strategy(self, name: str) -> Optional[BaseStrategy]:
        """获取指定策略实例"""
        return self._strategies.get(name)

    def list_strategies(self) -> List[str]:
        """列出所有已加载的策略名"""
        return list(self._strategies.keys())

    def reset_all(self):
        """重置所有策略状态"""
        for strategy in self._strategies.values():
            strategy.reset()

    @staticmethod
    def register_strategy(name: str, strategy_class: type):
        """注册自定义策略到全局注册表"""
        STRATEGY_REGISTRY[name] = strategy_class
        logger.info(f"Registered custom strategy: '{name}'")

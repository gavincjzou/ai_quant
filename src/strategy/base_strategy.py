"""
Base Strategy - 策略抽象基类
定义所有策略的统一接口规范。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd


class Signal(Enum):
    """交易信号类型"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """交易信号数据类"""
    symbol: str               # 标的代码，如 "AAPL.US"
    signal: Signal            # 交易方向
    price: float              # 当前/建议价格
    quantity: Optional[int] = None   # 建议数量（由仓位管理器最终决定）
    reason: str = ""          # 信号理由，用于日志记录
    confidence: float = 0.0   # 信号置信度 0.0 ~ 1.0
    strategy_name: str = ""   # 产生该信号的策略名称
    timestamp: Optional[str] = None  # 信号产生时间

    def __str__(self):
        return (
            f"[{self.strategy_name}] {self.signal.value} {self.symbol} "
            f"@ {self.price:.2f} | {self.reason} (conf={self.confidence:.2f})"
        )


class BaseStrategy(ABC):
    """
    所有策略的抽象基类。
    
    每个策略必须实现:
    - name: 策略名称
    - init(): 初始化（加载参数）
    - generate_signal(): 根据数据生成买卖信号
    - on_order_filled(): 成交回调
    """

    def __init__(self):
        self._name: str = ""
        self._params: Dict[str, Any] = {}
        self._state: Dict[str, Any] = {}  # 策略内部状态

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    def init(self, config: dict) -> None:
        """
        初始化策略参数。
        
        Args:
            config: 从 strategies.yaml 读取的该策略配置
        """
        pass

    @abstractmethod
    def generate_signal(self, symbol: str, data: pd.DataFrame) -> Optional[TradeSignal]:
        """
        根据历史数据生成交易信号。
        
        重要规则:
        - 只能使用 data 中已有的数据（禁止未来函数）
        - 信号基于最后一根已完成的 bar
        - 返回 None 表示 HOLD（无操作）
        
        Args:
            symbol: 标的代码
            data: OHLCV DataFrame，按时间升序排列
                  columns: [date, open, high, low, close, volume]
                  最后一行是最新的已完成 bar
                  
        Returns:
            TradeSignal or None
        """
        pass

    def on_order_filled(self, order_info: dict) -> None:
        """
        订单成交回调。可选实现。
        
        Args:
            order_info: 成交信息 dict，包含 symbol, side, quantity, price, time 等
        """
        pass

    def on_bar(self, symbol: str, bar: dict) -> None:
        """
        每根新 bar 回调。可选实现。
        用于更新策略内部状态。
        
        Args:
            symbol: 标的代码
            bar: 单根 bar 数据 dict {date, open, high, low, close, volume}
        """
        pass

    def reset(self) -> None:
        """重置策略状态（用于回测多轮运行）"""
        self._state.clear()

    def get_params(self) -> dict:
        """获取当前策略参数"""
        return self._params.copy()

    def __repr__(self):
        return f"<Strategy: {self.name}, params={self._params}>"

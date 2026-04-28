"""
RegimeDetector - 大盘市场状态识别器（阶段 11 P1-3）

用途：
    根据 SPY 当前价相对于 200 日均线（200MA）的位置，判定当前市场处于
    bull / bear / neutral 三种状态之一。在 RiskManager 内可据此调整
    新建仓的仓位倍率（如 bear 时单笔仓位 ×0.5）。

判定规则：
    - bull   ：SPY > 200MA × (1 + buffer)
    - bear   ：SPY < 200MA × (1 - buffer)
    - neutral：在两者之间
    - buffer 默认 0%（严格分界），可配 1%-3% 减少抖动

设计原则：
    - 纯只读分析，不主动产生交易信号
    - 200MA 算不出来时（K 线 < 200 根）→ 退化 neutral，不报错
    - 配置开关在 risk.yaml.timing.enabled，默认 false（保守上线）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional

import pandas as pd
from loguru import logger


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"


@dataclass
class RegimeStatus:
    regime: Regime
    spy_close: float
    spy_ma: float
    deviation_pct: float       # (close - ma) / ma
    as_of_date: Optional[date] = None
    reason: str = ""

    @property
    def is_bear(self) -> bool:
        return self.regime == Regime.BEAR

    @property
    def is_bull(self) -> bool:
        return self.regime == Regime.BULL

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "spy_close": self.spy_close,
            "spy_ma": self.spy_ma,
            "deviation_pct": self.deviation_pct,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "reason": self.reason,
        }


class RegimeDetector:
    """SPY-200MA 大盘状态识别器"""

    DEFAULT_MA_PERIOD = 200
    DEFAULT_BUFFER_PCT = 0.0
    DEFAULT_SYMBOL = "SPY.US"

    def __init__(self, config: Optional[dict] = None):
        """
        Args:
            config: 可选 timing config 段，含：
                - enabled: bool 默认 False
                - ma_period: int 默认 200
                - buffer_pct: float 默认 0.0
                - symbol: str 默认 'SPY.US'
                - bear_position_multiplier: float 默认 0.5（bear 时仓位倍率）
        """
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.ma_period = int(cfg.get("ma_period", self.DEFAULT_MA_PERIOD))
        self.buffer_pct = float(cfg.get("buffer_pct", self.DEFAULT_BUFFER_PCT))
        self.symbol = str(cfg.get("symbol", self.DEFAULT_SYMBOL))
        self.bear_multiplier = float(cfg.get("bear_position_multiplier", 0.5))

    @staticmethod
    def detect_from_series(
        close_series: pd.Series,
        ma_period: int = 200,
        buffer_pct: float = 0.0,
    ) -> RegimeStatus:
        """从 K 线 close Series 直接判定 regime（用于回测/测试）。

        Args:
            close_series: pandas Series，按时间升序，最后一个是最新收盘价
            ma_period: MA 周期
            buffer_pct: 上下缓冲带百分比

        Returns:
            RegimeStatus
        """
        if close_series is None or len(close_series) == 0:
            return RegimeStatus(
                regime=Regime.NEUTRAL, spy_close=0.0, spy_ma=0.0,
                deviation_pct=0.0, reason="empty close series",
            )

        if len(close_series) < ma_period:
            spy_close = float(close_series.iloc[-1])
            return RegimeStatus(
                regime=Regime.NEUTRAL,
                spy_close=spy_close, spy_ma=spy_close,
                deviation_pct=0.0,
                reason=f"K线不足 {ma_period} 根（仅 {len(close_series)}）→ 退化 neutral",
            )

        ma = close_series.rolling(ma_period).mean().iloc[-1]
        spy_close = float(close_series.iloc[-1])

        if not pd.notna(ma) or ma <= 0:
            return RegimeStatus(
                regime=Regime.NEUTRAL,
                spy_close=spy_close, spy_ma=spy_close,
                deviation_pct=0.0,
                reason="MA 计算失败（NaN）→ 退化 neutral",
            )

        deviation = (spy_close - ma) / ma
        upper = buffer_pct
        lower = -buffer_pct

        if deviation > upper:
            regime = Regime.BULL
        elif deviation < lower:
            regime = Regime.BEAR
        else:
            regime = Regime.NEUTRAL

        return RegimeStatus(
            regime=regime,
            spy_close=spy_close, spy_ma=float(ma),
            deviation_pct=float(deviation),
            reason=f"close={spy_close:.2f} vs MA{ma_period}={ma:.2f}, dev={deviation:+.2%}",
        )

    def detect(self, db=None) -> RegimeStatus:
        """从 DB 拉 SPY K 线后判定 regime。

        Args:
            db: DatabaseManager 实例（如不传，懒加载）

        Returns:
            RegimeStatus（即使数据缺失也返回 NEUTRAL 不抛异常）
        """
        try:
            if db is None:
                from src.data.database import DatabaseManager
                db = DatabaseManager()

            with db._get_conn() as conn:
                df = pd.read_sql_query(
                    """SELECT date, close FROM kline_data
                       WHERE symbol = ? AND period = '1d'
                       ORDER BY date ASC""",
                    conn, params=(self.symbol,),
                )
            if df.empty:
                return RegimeStatus(
                    regime=Regime.NEUTRAL, spy_close=0.0, spy_ma=0.0,
                    deviation_pct=0.0,
                    reason=f"DB 无 {self.symbol} K 线 → 退化 neutral",
                )

            df["date"] = pd.to_datetime(df["date"])
            close = df["close"].astype(float)
            status = self.detect_from_series(close, self.ma_period, self.buffer_pct)
            status.as_of_date = df["date"].iloc[-1].date()
            return status
        except Exception as e:
            logger.warning(f"[RegimeDetector] detect 失败: {e}")
            return RegimeStatus(
                regime=Regime.NEUTRAL, spy_close=0.0, spy_ma=0.0,
                deviation_pct=0.0, reason=f"detect 异常: {e}",
            )

    def get_position_multiplier(self, status: Optional[RegimeStatus] = None) -> float:
        """根据当前 regime 返回仓位倍率（用于 RiskManager 调整 target_amount）。

        - 未启用 → 1.0（不变）
        - bull → 1.0
        - neutral → 1.0
        - bear → bear_multiplier（默认 0.5）
        """
        if not self.enabled:
            return 1.0
        if status is None:
            status = self.detect()
        if status.is_bear:
            return self.bear_multiplier
        return 1.0

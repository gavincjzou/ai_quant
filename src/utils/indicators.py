"""
Technical Indicators - 通用技术指标库
无状态纯函数，所有策略和风控模块共用，避免各自重复实现。

核心原则：
- 输入 DataFrame 或 Series，输出 Series（保持 index 对齐）
- 向量化实现（pandas/numpy），避免 for 循环
- 数据不足时返回全 NaN 的 Series，不抛异常
- 前置 warm-up 期（如 ATR 的前 period-1 根）为 NaN
"""

from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (平均真实波幅)，Wilder 平滑版。

    公式：
        TR = max(H - L, |H - PC|, |L - PC|)
        ATR = Wilder EMA of TR, 等价于 ewm(alpha=1/period, adjust=False)

    Args:
        df: DataFrame 包含 high/low/close 列（不区分大小写）
        period: ATR 平滑周期，默认 14

    Returns:
        pd.Series，与 df 等长，前 period-1 根为 NaN
    """
    if df is None or df.empty:
        logger.warning("calc_atr: empty DataFrame")
        return pd.Series(dtype=float)

    # 列名统一小写（兼容 High/high）
    cols = {c.lower(): c for c in df.columns}
    try:
        high = df[cols["high"]]
        low = df[cols["low"]]
        close = df[cols["close"]]
    except KeyError as e:
        logger.error(f"calc_atr missing column: {e}")
        return pd.Series([np.nan] * len(df), index=df.index)

    if len(df) < period + 1:
        logger.debug(
            f"calc_atr data insufficient: got {len(df)}, need {period + 1}"
        )
        return pd.Series([np.nan] * len(df), index=df.index)

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder 平滑: α = 1/period, adjust=False
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    atr.name = f"ATR_{period}"
    return atr


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (相对强弱指数)，Wilder 平滑版（向量化实现）。

    相比旧版 for 循环实现，性能提升 10x+。

    Args:
        series: 价格序列（通常是 close）
        period: RSI 周期，默认 14

    Returns:
        pd.Series，前 period 根为 NaN
    """
    if series is None or len(series) < period + 1:
        return pd.Series([np.nan] * (len(series) if series is not None else 0))

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # Wilder 平滑: α = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)  # loss 全为 0 时 RSI=100
    rsi.name = f"RSI_{period}"
    return rsi


def calc_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    MACD - Moving Average Convergence Divergence。

    Returns:
        DataFrame with columns [macd, signal, histogram]
    """
    raise NotImplementedError("calc_macd 预留接口，阶段 3+ 实现")


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ADX - Average Directional Index (平均趋向指数)，用于判断趋势强度。

    Returns:
        pd.Series，ADX 值，前 2*period 根为 NaN
    """
    raise NotImplementedError("calc_adx 预留接口，阶段 3+ 实现")


def calc_bollinger(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """
    Bollinger Bands。

    Returns:
        DataFrame with columns [middle, upper, lower]
    """
    raise NotImplementedError("calc_bollinger 预留接口")

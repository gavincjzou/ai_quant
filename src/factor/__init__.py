"""阶段 9：多因子选股模块

V0 版本：基于 LongPort calc_indexes 的 4 因子打分模型
- Value（价值）：PE_TTM + PB + Dividend
- Momentum（动量）：5 日涨幅 + 半年涨幅
- Size（规模）：总市值（偏中盘）
- Liquidity（流动性/技术）：换手率

未来版本可扩展：
- Quality 因子（需要财报数据源如 yfinance）
- Technical 因子（RSI/MACD 等需历史 K 线计算）
- 多因子组合优化 + 再平衡
"""
from src.factor.factor_fetcher import FactorFetcher
from src.factor.factor_scorer import FactorScorer

__all__ = ["FactorFetcher", "FactorScorer"]

"""阶段 9：多因子选股模块

V0 版本（保留）：基于 LongPort calc_indexes 的 4 因子打分模型
- Value（价值）：PE_TTM + PB + Dividend
- Momentum（动量）：5 日涨幅 + 半年涨幅
- Size（规模）：总市值（偏中盘）
- Liquidity（流动性/技术）：换手率

V1 版本（2026-04-25）：六因子打分，反映 AI 热点偏好
- 权重调整：Value 15% / Momentum 35% / Quality 15% / Size 5% / Liquidity 15% / Industry 15%
- Quality 因子新增：NetMargin + GrossMargin + OperatingMargin + ROE（Westock 数据源）
- Industry 因子新增：行业 σ bias（半导体 +1.0σ，AI 基建 +0.8σ 等）
- ETF 特殊通道：SPY/QQQ/IWM 只走 Value+Momentum+Size 三因子

未来版本可扩展：
- Technical 因子（RSI/MACD 等需历史 K 线计算）
- 多因子组合优化 + 再平衡
"""
from src.factor.factor_fetcher import FactorFetcher
from src.factor.factor_scorer import FactorScorer
from src.factor.v1_scorer import V1Scorer
from src.factor.industry_map import get_industry_bias

__all__ = ["FactorFetcher", "FactorScorer", "V1Scorer", "get_industry_bias"]

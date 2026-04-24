"""FactorScorer - 因子打分器

职责：
1. 对原始指标做 winsorized Z-Score 标准化（截断 5%-95% 分位数避免极端值）
2. 按 4 大类因子加权聚合
3. 输出 total_score + rank + 因子分解

阶段 9 V0 设计决策：
- Value 因子 25%（PE 倒序 + PB 倒序 + Dividend 正序）
- Momentum 因子 30%（5 日 40% + 半年 60%）
- Size 因子 15%（log10 后反向，偏向中小市值）
- Liquidity 因子 30%（换手率反向，避免炒作股）
- Quality 因子暂无（LongPort SDK 无财报 API，权重 0）

所有 z-score 都做 winsorize 截断（1% 和 99% 分位数）避免极端值扭曲标准差。
"""
from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger


class FactorScorer:
    """四因子打分器"""

    # 默认权重（和 = 1.0）
    DEFAULT_WEIGHTS: Dict[str, float] = {
        "value": 0.25,
        "momentum": 0.30,
        "size": 0.15,
        "liquidity": 0.30,
    }

    # Value 子因子权重（和 = 1.0）
    VALUE_SUB_WEIGHTS = {
        "pe_ttm_ratio": 0.4,   # PE 反向
        "pb_ratio": 0.4,        # PB 反向
        "dividend_ratio_ttm": 0.2,  # Div 正向
    }

    # Momentum 子因子权重
    MOMENTUM_SUB_WEIGHTS = {
        "five_day_change_rate": 0.4,
        "half_year_change_rate": 0.6,
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        # 校验
        total = sum(self.weights.values())
        if abs(total - 1.0) > 0.01:
            logger.warning(f"[FactorScorer] 权重和为 {total}，非 1.0，会归一化")
            self.weights = {k: v / total for k, v in self.weights.items()}

    # ------------------------------------------------------------
    # 归一化工具
    # ------------------------------------------------------------

    @staticmethod
    def winsorize(s: pd.Series, lower_q: float = 0.05, upper_q: float = 0.95) -> pd.Series:
        """
        截断极端值（winsorize）。把超出 [lower_q, upper_q] 分位数的值压到边界。

        小样本（N<30）时用更保守的分位数 5%/95%（而非常用的 1%/99%），
        避免单个异常值完全扭曲结果。
        """
        valid = s.dropna()
        if len(valid) < 3:
            return s
        ql, qh = valid.quantile(lower_q), valid.quantile(upper_q)
        return s.clip(ql, qh)

    @classmethod
    def zscore(cls, s: pd.Series, winsorize: bool = True) -> pd.Series:
        """
        Winsorized Z-Score 截面标准化。

        步骤：
        1. 可选 winsorize 截断（默认开）
        2. (x - mean) / std
        3. 再 clip 到 [-3, 3] 防止异常值

        NaN 保留为 NaN（上层按 0 处理）。
        """
        s = s.astype(float).copy()
        if winsorize:
            s = cls.winsorize(s)
        std = s.std(ddof=0)
        if std is None or std < 1e-9 or pd.isna(std):
            return pd.Series(0.0, index=s.index)
        z = (s - s.mean()) / std
        return z.clip(-3.0, 3.0)

    @staticmethod
    def fill_na_with_zero(s: pd.Series) -> pd.Series:
        """缺失值填 0（对应中性分）"""
        return s.fillna(0.0)

    # ------------------------------------------------------------
    # 因子计算
    # ------------------------------------------------------------

    def _value_score(self, df: pd.DataFrame) -> pd.Series:
        """
        价值因子：低 PE、低 PB、高股息 好。
        PE < 0（亏损公司）按 0 分处理。
        """
        pe = df["pe_ttm_ratio"].copy()
        # 亏损公司 PE < 0 当缺失处理（避免被当成"便宜"）
        pe.loc[pe <= 0] = np.nan

        pe_z = -self.zscore(pe)                       # 低 PE 好 → 反向
        pb_z = -self.zscore(df["pb_ratio"])            # 低 PB 好 → 反向
        div_z = self.zscore(df["dividend_ratio_ttm"])  # 高股息好 → 正向

        combined = (
            self.VALUE_SUB_WEIGHTS["pe_ttm_ratio"] * self.fill_na_with_zero(pe_z)
            + self.VALUE_SUB_WEIGHTS["pb_ratio"] * self.fill_na_with_zero(pb_z)
            + self.VALUE_SUB_WEIGHTS["dividend_ratio_ttm"] * self.fill_na_with_zero(div_z)
        )
        return combined

    def _momentum_score(self, df: pd.DataFrame) -> pd.Series:
        """动量因子：涨得多好。"""
        short_z = self.zscore(df["five_day_change_rate"])
        long_z = self.zscore(df["half_year_change_rate"])

        combined = (
            self.MOMENTUM_SUB_WEIGHTS["five_day_change_rate"] * self.fill_na_with_zero(short_z)
            + self.MOMENTUM_SUB_WEIGHTS["half_year_change_rate"] * self.fill_na_with_zero(long_z)
        )
        return combined

    def _size_score(self, df: pd.DataFrame) -> pd.Series:
        """
        规模因子：偏向中小市值（学术上中小盘长期跑赢）。
        用 log(MV) 归一化，避免市值数量级差异拉偏。
        """
        mv = df["total_market_value"].copy()
        # log 转换前先过滤非正值
        mv.loc[mv <= 0] = np.nan
        log_mv = np.log10(mv)
        # 反向：市值越大分越低
        return -self.fill_na_with_zero(self.zscore(log_mv))

    def _liquidity_score(self, df: pd.DataFrame) -> pd.Series:
        """
        流动性因子：换手率反向（避免资金炒作的过热股）。
        合理的换手应该是"有成交但不狂热"。
        """
        turnover = df["turnover_rate"].copy()
        # 换手率反向（越低越好，避免过热）
        return -self.fill_na_with_zero(self.zscore(turnover))

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------

    def score(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        """
        输入：原始指标 DataFrame（FactorFetcher 产出）
        输出：DataFrame 追加 value_score / momentum_score / size_score /
              liquidity_score / total_score / rank 列，按 total_score 降序
        """
        if raw_df is None or raw_df.empty:
            logger.warning("[FactorScorer] 输入为空")
            return raw_df

        df = raw_df.copy()

        # 4 因子得分
        df["value_score"] = self._value_score(df).round(4)
        df["momentum_score"] = self._momentum_score(df).round(4)
        df["size_score"] = self._size_score(df).round(4)
        df["liquidity_score"] = self._liquidity_score(df).round(4)

        # 总分 = 加权求和
        df["total_score"] = (
            self.weights["value"] * df["value_score"]
            + self.weights["momentum"] * df["momentum_score"]
            + self.weights["size"] * df["size_score"]
            + self.weights["liquidity"] * df["liquidity_score"]
        ).round(4)

        # 排名（总分高 → rank 小）
        df = df.sort_values("total_score", ascending=False).reset_index(drop=True)
        df["rank"] = df.index + 1

        logger.info(f"[FactorScorer] 打分完成：{len(df)} 只标的，"
                    f"Top-1: {df.iloc[0]['symbol']} (score={df.iloc[0]['total_score']:.3f})")
        return df

    def score_and_store(
        self,
        raw_df: pd.DataFrame,
        db,
        snapshot_date: str,
    ) -> pd.DataFrame:
        """打分 + 把得分回写到 factor_snapshots 表（更新 value_score/.../rank 等字段）"""
        scored = self.score(raw_df)
        if scored is None or scored.empty:
            return scored

        snapshots = []
        for _, row in scored.iterrows():
            snap = {
                "date": snapshot_date,
                "symbol": row["symbol"],
            }
            # 原始指标（从 raw_df）
            for f in ["pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm",
                      "total_market_value", "five_day_change_rate",
                      "half_year_change_rate", "turnover_rate"]:
                v = row.get(f)
                snap[f] = None if (v is None or (isinstance(v, float) and v != v)) else float(v)
            # 得分
            for f in ["value_score", "momentum_score", "size_score",
                      "liquidity_score", "total_score"]:
                snap[f] = float(row[f])
            snap["rank"] = int(row["rank"])
            snapshots.append(snap)

        db.save_factor_snapshots_batch(snapshots)
        logger.info(f"[FactorScorer] 得分已回写 {len(snapshots)} 条到 DB")
        return scored

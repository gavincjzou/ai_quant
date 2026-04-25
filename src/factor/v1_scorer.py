"""V1Scorer - 阶段 9 V1 六因子打分器

继承 FactorScorer，新增：
1. Quality 因子（NetMargin + GrossMargin + OperatingMargin + ROE 加权）
2. Industry 因子（行业 σ bias，AI 热点偏好）
3. ETF 特殊通道（跳过 Quality/Industry/Liquidity，只走 Value/Momentum/Size）
4. score_v1 主入口

权重（反映 Gavin "AI 时代 PE 低不等于好" + "偏好行业热点"）：
- Value 15%（降权，V0 是 25%）
- Momentum 35%（提权，V0 是 30%）
- Quality 15%（新增）
- Size 5%（降权，V0 是 15%）
- Liquidity 15%（降权，V0 是 30%，修 ETF 误伤）
- Industry 15%（新增）

ETF 特殊权重（只用 3 因子）：
- Value 30% + Momentum 50% + Size 20%
"""
from __future__ import annotations

from typing import Dict, Optional, Set

import numpy as np
import pandas as pd
from loguru import logger

from src.factor.factor_scorer import FactorScorer
from src.factor.industry_map import get_industry_bias


class V1Scorer(FactorScorer):
    """六因子打分器（V1）"""

    # V1 权重（和 = 1.0）
    V1_WEIGHTS: Dict[str, float] = {
        "value": 0.15,
        "momentum": 0.35,
        "quality": 0.15,
        "size": 0.05,
        "liquidity": 0.15,
        "industry": 0.15,
    }

    # ETF 特殊通道权重（跳过 quality/liquidity/industry，和 = 1.0）
    ETF_WEIGHTS: Dict[str, float] = {
        "value": 0.30,
        "momentum": 0.50,
        "size": 0.20,
    }

    # ETF 白名单（3 只基准 ETF）
    ETF_SYMBOLS: Set[str] = {"SPY.US", "QQQ.US", "IWM.US"}

    # Quality 子因子权重（和 = 1.0）
    QUALITY_SUB_WEIGHTS = {
        "net_margin": 0.30,        # 净利率
        "gross_margin": 0.20,      # 毛利率
        "operating_margin": 0.20,  # 营业利润率
        "roe": 0.30,               # 真 Quality 核心指标
    }

    def __init__(self):
        # 不调 super().__init__，因为 V1 权重和 V0 不同
        self.weights = self.V1_WEIGHTS.copy()
        total = sum(self.weights.values())
        if abs(total - 1.0) > 0.01:
            logger.warning(f"[V1Scorer] V1_WEIGHTS 和为 {total}，非 1.0，自动归一化")
            self.weights = {k: v / total for k, v in self.weights.items()}

    # ------------------------------------------------------------
    # V1 新增因子
    # ------------------------------------------------------------

    def _quality_score(self, df: pd.DataFrame) -> pd.Series:
        """
        Quality 因子：净利率/毛利率/营业利润率/ROE 四合一。

        所有子因子都是"越大越好"方向（正向）。
        缺失字段给 0 分（中性）——由 fill_na_with_zero 处理。
        """
        parts = []
        for field, weight in self.QUALITY_SUB_WEIGHTS.items():
            if field in df.columns:
                z = self.zscore(df[field])
                parts.append(weight * self.fill_na_with_zero(z))
            else:
                logger.warning(f"[V1Scorer] Quality 字段缺失: {field}，权重 {weight} 丢失")

        if not parts:
            return pd.Series(0.0, index=df.index)
        return sum(parts)

    def _industry_score(self, df: pd.DataFrame) -> pd.Series:
        """
        Industry 因子：按 industry_map 返回 σ bias。

        注意：这是**静态 bias**，不过 zscore（避免归一化抵消加权意图）。
        直接在 total_score 阶段以 σ 单位叠加。
        """
        biases = []
        for _, row in df.iterrows():
            symbol = row["symbol"]
            industry_zh = row.get("industry") if "industry" in df.columns else None
            industry_en = row.get("industry_en") if "industry_en" in df.columns else None
            bias = get_industry_bias(symbol, industry_zh, industry_en)
            biases.append(bias)
        # 截断到 [-3, 3] 保持和其他因子量纲一致
        return pd.Series(biases, index=df.index).clip(-3.0, 3.0)

    # ------------------------------------------------------------
    # V1 主入口
    # ------------------------------------------------------------

    def score_v1(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        """
        V1 主打分入口。

        1. 先分离 ETF 和普通标的
        2. ETF 走 ETF_WEIGHTS 三因子通道
        3. 普通标的走 V1_WEIGHTS 六因子通道
        4. 合并 + 重新 rank + 输出

        Args:
            raw_df: FactorFetcher 合并后的原始指标 DataFrame
                    columns: symbol + V0 七字段 +
                             net_margin/gross_margin/operating_margin/roe +
                             sector/industry(_zh)/industry_en

        Returns:
            DataFrame 追加所有因子得分 + total_score + rank，按 total_score 降序
        """
        if raw_df is None or raw_df.empty:
            logger.warning("[V1Scorer] 输入为空")
            return raw_df

        df = raw_df.copy()

        # 分离 ETF vs 普通
        is_etf = df["symbol"].isin(self.ETF_SYMBOLS)
        normal = df[~is_etf].copy()
        etfs = df[is_etf].copy()

        # === 普通标的：六因子 ===
        if not normal.empty:
            normal["value_score"] = self._value_score(normal).round(4)
            normal["momentum_score"] = self._momentum_score(normal).round(4)
            normal["quality_score"] = self._quality_score(normal).round(4)
            normal["size_score"] = self._size_score(normal).round(4)
            normal["liquidity_score"] = self._liquidity_score(normal).round(4)
            normal["industry_score"] = self._industry_score(normal).round(4)

            normal["total_score"] = (
                self.V1_WEIGHTS["value"] * normal["value_score"]
                + self.V1_WEIGHTS["momentum"] * normal["momentum_score"]
                + self.V1_WEIGHTS["quality"] * normal["quality_score"]
                + self.V1_WEIGHTS["size"] * normal["size_score"]
                + self.V1_WEIGHTS["liquidity"] * normal["liquidity_score"]
                + self.V1_WEIGHTS["industry"] * normal["industry_score"]
            ).round(4)

        # === ETF：三因子 ===
        if not etfs.empty:
            etfs["value_score"] = self._value_score(etfs).round(4)
            etfs["momentum_score"] = self._momentum_score(etfs).round(4)
            etfs["size_score"] = self._size_score(etfs).round(4)
            # 其他因子给 0（避免 NaN 影响合并）
            etfs["quality_score"] = 0.0
            etfs["liquidity_score"] = 0.0
            etfs["industry_score"] = 0.0

            etfs["total_score"] = (
                self.ETF_WEIGHTS["value"] * etfs["value_score"]
                + self.ETF_WEIGHTS["momentum"] * etfs["momentum_score"]
                + self.ETF_WEIGHTS["size"] * etfs["size_score"]
            ).round(4)

        # 合并
        result = pd.concat([normal, etfs], ignore_index=True)

        # 全局 rank
        result = result.sort_values("total_score", ascending=False).reset_index(drop=True)
        result["rank"] = result.index + 1

        logger.info(
            f"[V1Scorer] V1 打分完成：{len(result)} 只标的（{len(normal)} 普通 + {len(etfs)} ETF），"
            f"Top-1: {result.iloc[0]['symbol']} (score={result.iloc[0]['total_score']:.3f})"
        )
        return result

    def score_and_store_v1(
        self,
        raw_df: pd.DataFrame,
        db,
        snapshot_date: str,
    ) -> pd.DataFrame:
        """
        V1 打分 + 回写到 factor_snapshots 表（version='v1'）。

        用 save_factor_snapshot（逐条），因为批量版不支持新字段。
        """
        scored = self.score_v1(raw_df)
        if scored is None or scored.empty:
            return scored

        for _, row in scored.iterrows():
            snap = {
                "date": snapshot_date,
                "symbol": row["symbol"],
                "version": "v1",
            }
            # V0 原始指标
            for f in ["pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm",
                      "total_market_value", "five_day_change_rate",
                      "half_year_change_rate", "turnover_rate"]:
                v = row.get(f)
                snap[f] = None if (v is None or (isinstance(v, float) and pd.isna(v))) else float(v)
            # V1 新字段
            for f in ["sector", "industry"]:
                v = row.get(f)
                snap[f] = None if v is None else str(v)
            for f in ["net_margin", "gross_margin", "revenue_growth"]:
                v = row.get(f)
                snap[f] = None if (v is None or (isinstance(v, float) and pd.isna(v))) else float(v)
            # 得分
            for f in ["value_score", "momentum_score", "size_score",
                      "liquidity_score", "quality_score", "industry_score",
                      "total_score"]:
                v = row.get(f)
                snap[f] = None if (v is None or (isinstance(v, float) and pd.isna(v))) else float(v)
            snap["rank"] = int(row["rank"])

            self._save_v1_snapshot(db, snap)

        logger.info(f"[V1Scorer] {len(scored)} 条 V1 snapshot 已写入 DB")
        return scored

    @staticmethod
    def _save_v1_snapshot(db, snap: dict):
        """写单条 V1 snapshot 到 factor_snapshots 表"""
        with db._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO factor_snapshots (
                    date, symbol, version,
                    pe_ttm_ratio, pb_ratio, dividend_ratio_ttm, total_market_value,
                    five_day_change_rate, half_year_change_rate, turnover_rate,
                    sector, industry, net_margin, gross_margin, revenue_growth,
                    value_score, momentum_score, size_score, liquidity_score,
                    quality_score, industry_score, total_score, rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap["date"], snap["symbol"], snap["version"],
                    snap.get("pe_ttm_ratio"), snap.get("pb_ratio"),
                    snap.get("dividend_ratio_ttm"), snap.get("total_market_value"),
                    snap.get("five_day_change_rate"), snap.get("half_year_change_rate"),
                    snap.get("turnover_rate"),
                    snap.get("sector"), snap.get("industry"),
                    snap.get("net_margin"), snap.get("gross_margin"),
                    snap.get("revenue_growth"),
                    snap.get("value_score"), snap.get("momentum_score"),
                    snap.get("size_score"), snap.get("liquidity_score"),
                    snap.get("quality_score"), snap.get("industry_score"),
                    snap.get("total_score"), snap.get("rank"),
                ),
            )

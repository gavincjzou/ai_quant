"""
test_factor.py - V1Scorer + IndustryMap 单元测试

覆盖：
- V1Scorer.score_v1 happy path
- ETF 特殊三因子通道
- Quality 字段缺失时的容错
- Industry 因子 bias 直接叠加（不过 zscore）
- get_industry_bias 中文/英文/symbol 三级 fallback
- winsorize Z-Score 处理异常值
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import sample_factor_raw_df

from src.factor.v1_scorer import V1Scorer
from src.factor.industry_map import (
    get_industry_bias,
    INDUSTRY_BIAS_ZH,
    INDUSTRY_BIAS_EN,
    SYMBOL_BIAS_FALLBACK,
)


class TestV1ScorerHappyPath(unittest.TestCase):
    """V1Scorer 主流程测试"""

    def setUp(self):
        self.scorer = V1Scorer()
        self.df = sample_factor_raw_df()

    def test_v1_weights_sum_to_one(self):
        """V1 权重和应该 = 1.0"""
        total = sum(V1Scorer.V1_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_etf_weights_sum_to_one(self):
        """ETF 权重和应该 = 1.0"""
        total = sum(V1Scorer.ETF_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_score_v1_returns_dataframe(self):
        """score_v1 应返回带得分的 DataFrame"""
        result = self.scorer.score_v1(self.df)
        self.assertIsInstance(result, pd.DataFrame)
        self.assertGreater(len(result), 0)

    def test_score_v1_has_required_columns(self):
        """输出应包含所有因子得分列"""
        result = self.scorer.score_v1(self.df)
        required = ["total_score", "rank", "value_score", "momentum_score",
                    "quality_score", "industry_score", "size_score", "liquidity_score"]
        for col in required:
            self.assertIn(col, result.columns, f"缺失列: {col}")

    def test_score_v1_rank_is_unique_and_continuous(self):
        """rank 应该是 1..N 连续整数"""
        result = self.scorer.score_v1(self.df)
        ranks = sorted(result["rank"].tolist())
        self.assertEqual(ranks, list(range(1, len(self.df) + 1)))

    def test_score_v1_sorted_by_total_score_desc(self):
        """结果应按 total_score 降序排"""
        result = self.scorer.score_v1(self.df)
        scores = result["total_score"].tolist()
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_v1_empty_input_returns_empty(self):
        """空 DataFrame 输入应返回空"""
        empty = pd.DataFrame()
        result = self.scorer.score_v1(empty)
        self.assertTrue(result.empty)


class TestV1ScorerETFChannel(unittest.TestCase):
    """ETF 特殊通道测试"""

    def setUp(self):
        self.scorer = V1Scorer()

    def test_etf_uses_three_factor_channel(self):
        """ETF 标的的 quality/industry/liquidity 应为 0"""
        df = sample_factor_raw_df(symbols=["NVDA.US", "QQQ.US", "MU.US"])
        result = self.scorer.score_v1(df)

        qqq_row = result[result["symbol"] == "QQQ.US"].iloc[0]
        # ETF 跳过 Quality 和 Industry
        self.assertEqual(qqq_row["quality_score"], 0.0)
        self.assertEqual(qqq_row["industry_score"], 0.0)
        self.assertEqual(qqq_row["liquidity_score"], 0.0)

    def test_non_etf_has_quality_and_industry(self):
        """非 ETF 应该有 quality/industry 得分（非零）"""
        df = sample_factor_raw_df(symbols=["NVDA.US", "MU.US", "JPM.US"])
        result = self.scorer.score_v1(df)

        nvda_row = result[result["symbol"] == "NVDA.US"].iloc[0]
        # 半导体 industry bias = +1.0σ
        self.assertGreater(nvda_row["industry_score"], 0.5)


class TestV1ScorerQualityFallback(unittest.TestCase):
    """Quality 字段缺失容错"""

    def setUp(self):
        self.scorer = V1Scorer()

    def test_missing_roe_does_not_crash(self):
        """ROE 字段全 NaN 时不应崩溃"""
        df = sample_factor_raw_df()
        df["roe"] = np.nan
        result = self.scorer.score_v1(df)
        self.assertGreater(len(result), 0)
        # quality_score 仍然能算（其他三个子因子还在）
        self.assertIn("quality_score", result.columns)

    def test_missing_quality_columns_entirely(self):
        """完全没有 Quality 字段（V0 数据）也不崩"""
        df = sample_factor_raw_df(include_quality=False)
        result = self.scorer.score_v1(df)
        self.assertGreater(len(result), 0)
        # quality_score 应该全 0
        self.assertTrue((result["quality_score"] == 0.0).all())


class TestV1ScorerIndustryBias(unittest.TestCase):
    """Industry 因子按 σ 直接叠加"""

    def setUp(self):
        self.scorer = V1Scorer()

    def test_semiconductor_gets_positive_bias(self):
        """半导体应得 +1.0σ"""
        df = sample_factor_raw_df(symbols=["MU.US", "JNJ.US"])
        result = self.scorer.score_v1(df)
        mu_industry = result[result["symbol"] == "MU.US"]["industry_score"].iloc[0]
        jnj_industry = result[result["symbol"] == "JNJ.US"]["industry_score"].iloc[0]
        self.assertGreater(mu_industry, jnj_industry)

    def test_industry_score_clipped_to_3sigma(self):
        """industry_score 被截断到 [-3, 3]"""
        df = sample_factor_raw_df()
        result = self.scorer.score_v1(df)
        self.assertTrue((result["industry_score"] >= -3.0).all())
        self.assertTrue((result["industry_score"] <= 3.0).all())


class TestIndustryMap(unittest.TestCase):
    """get_industry_bias 三级 fallback 测试"""

    def test_chinese_industry_lookup(self):
        """中文 industry 应优先匹配"""
        bias = get_industry_bias("MU.US", industry_zh="半导体")
        self.assertEqual(bias, INDUSTRY_BIAS_ZH["半导体"])

    def test_english_industry_lookup(self):
        """无中文时英文应匹配"""
        # 找一个 INDUSTRY_BIAS_EN 里有的 key
        if INDUSTRY_BIAS_EN:
            en_key = next(iter(INDUSTRY_BIAS_EN.keys()))
            expected = INDUSTRY_BIAS_EN[en_key]
            bias = get_industry_bias("FAKE.US", industry_zh=None, industry_en=en_key)
            self.assertEqual(bias, expected)

    def test_symbol_fallback(self):
        """中英都没时按 symbol 兜底"""
        # SYMBOL_BIAS_FALLBACK 里取一个
        if SYMBOL_BIAS_FALLBACK:
            sym = next(iter(SYMBOL_BIAS_FALLBACK.keys()))
            expected = SYMBOL_BIAS_FALLBACK[sym]
            bias = get_industry_bias(sym, industry_zh=None, industry_en=None)
            self.assertEqual(bias, expected)

    def test_unknown_returns_zero(self):
        """完全未知应返回 0.0（中性）"""
        bias = get_industry_bias(
            "ABSOLUTELYUNKNOWN.US",
            industry_zh="火星生物科技",
            industry_en="Mars Bio",
        )
        self.assertEqual(bias, 0.0)

    def test_empty_industry_zh_falls_through(self):
        """industry_zh 是 '-' 或空字符串应跳到下一级"""
        bias = get_industry_bias("MU.US", industry_zh="-", industry_en=None)
        # 应该走到 SYMBOL_BIAS_FALLBACK
        if "MU.US" in SYMBOL_BIAS_FALLBACK:
            self.assertEqual(bias, SYMBOL_BIAS_FALLBACK["MU.US"])

    def test_etf_industry_is_zero(self):
        """SPY/QQQ/IWM 的 industry bias 应为 0"""
        for etf in ["SPY.US", "QQQ.US", "IWM.US"]:
            bias = get_industry_bias(etf, industry_zh=None, industry_en=None)
            self.assertEqual(bias, 0.0, f"{etf} bias 应为 0")


if __name__ == "__main__":
    unittest.main()

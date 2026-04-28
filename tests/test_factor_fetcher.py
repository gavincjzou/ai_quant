"""
test_factor_fetcher.py - FactorFetcher 单元测试

覆盖：
- fetch（仅 V0 七字段，调 LongPort）
- fetch_v1（双源合并：LongPort + Westock）
- Westock 异常时降级（保留 LongPort 数据）
- LongPort 返回空时
- 合并后字段对齐
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.factor.factor_fetcher import FactorFetcher


def _mock_longport_df(symbols):
    """构造 LongPort fetch_calc_indexes 风格的返回"""
    n = len(symbols)
    return pd.DataFrame({
        "symbol": symbols,
        "pe_ttm_ratio": [25.0] * n,
        "pb_ratio": [3.5] * n,
        "dividend_ratio_ttm": [0.02] * n,
        "total_market_value": [1e11] * n,
        "five_day_change_rate": [0.01] * n,
        "half_year_change_rate": [0.15] * n,
        "turnover_rate": [0.02] * n,
    })


def _mock_westock_df(symbols):
    """构造 WestockClient.batch_fetch 返回"""
    n = len(symbols)
    return pd.DataFrame({
        "symbol": symbols,
        "sector": ["电子技术"] * n,
        "industry": ["半导体"] * n,
        "company_name": ["Test Corp"] * n,
        "net_margin": [22.5] * n,
        "gross_margin": [55.0] * n,
        "operating_margin": [28.0] * n,
        "roe": [18.0] * n,
        "liability_to_asset": [35.0] * n,
    })


class TestFactorFetcherV0(unittest.TestCase):
    """fetch（V0 七字段）"""

    def setUp(self):
        self.lp = MagicMock()
        self.db = MagicMock()
        self.fetcher = FactorFetcher(self.lp, self.db)

    def test_fetch_passes_indices_to_longport(self):
        """fetch 应该把 7 个 INDEX 名传给 LongPort"""
        self.lp.fetch_calc_indexes.return_value = _mock_longport_df(["NVDA.US"])
        df = self.fetcher.fetch(["NVDA.US"])
        self.lp.fetch_calc_indexes.assert_called_once()
        args = self.lp.fetch_calc_indexes.call_args
        self.assertEqual(args[0][0], ["NVDA.US"])
        # 第二个参数应是 RAW_INDEX_NAMES（7 个）
        self.assertEqual(len(args[0][1]), 7)

    def test_fetch_returns_dataframe(self):
        self.lp.fetch_calc_indexes.return_value = _mock_longport_df(["NVDA.US", "MU.US"])
        df = self.fetcher.fetch(["NVDA.US", "MU.US"])
        self.assertEqual(len(df), 2)


class TestFactorFetcherV1Merge(unittest.TestCase):
    """fetch_v1（双源合并）"""

    def setUp(self):
        self.lp = MagicMock()
        self.db = MagicMock()
        self.fetcher = FactorFetcher(self.lp, self.db)

    @patch("src.data.westock_client.WestockClient")
    def test_fetch_v1_merges_both_sources(self, mock_ws_class):
        """V0 七字段 + Westock 基本面应正确 merge"""
        symbols = ["NVDA.US", "MU.US"]
        self.lp.fetch_calc_indexes.return_value = _mock_longport_df(symbols)

        # mock westock instance
        mock_ws = MagicMock()
        mock_ws.ready = True
        mock_ws.batch_fetch.return_value = _mock_westock_df(symbols)
        mock_ws_class.return_value = mock_ws

        df = self.fetcher.fetch_v1(symbols)

        # 应包含两边的字段
        self.assertEqual(len(df), 2)
        self.assertIn("pe_ttm_ratio", df.columns)  # 来自 LongPort
        self.assertIn("net_margin", df.columns)    # 来自 Westock
        self.assertIn("roe", df.columns)
        self.assertIn("industry", df.columns)

    @patch("src.data.westock_client.WestockClient")
    def test_fetch_v1_falls_back_when_westock_not_ready(self, mock_ws_class):
        """Westock 未 ready 时只返回 V0 数据"""
        symbols = ["NVDA.US"]
        self.lp.fetch_calc_indexes.return_value = _mock_longport_df(symbols)

        mock_ws = MagicMock()
        mock_ws.ready = False
        mock_ws_class.return_value = mock_ws

        df = self.fetcher.fetch_v1(symbols)
        # V0 数据应该有
        self.assertEqual(len(df), 1)
        self.assertIn("pe_ttm_ratio", df.columns)

    @patch("src.data.westock_client.WestockClient")
    def test_fetch_v1_handles_westock_exception(self, mock_ws_class):
        """Westock 抛异常时不应崩主流程，仍返回 V0 数据"""
        symbols = ["NVDA.US"]
        self.lp.fetch_calc_indexes.return_value = _mock_longport_df(symbols)

        mock_ws = MagicMock()
        mock_ws.ready = True
        mock_ws.batch_fetch.side_effect = RuntimeError("network down")
        mock_ws_class.return_value = mock_ws

        df = self.fetcher.fetch_v1(symbols)
        # 不崩 + V0 数据保留
        self.assertEqual(len(df), 1)

    def test_fetch_v1_empty_input(self):
        """LongPort 返回空时应优雅处理"""
        self.lp.fetch_calc_indexes.return_value = pd.DataFrame()
        df = self.fetcher.fetch_v1([])
        # 空输入应返回空（或不崩）
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main()

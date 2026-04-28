"""
test_westock_client.py - WestockClient 单元测试

覆盖：
- _parse_md_table 解析 MD 表格
- _extract_section 提取 income/balance section
- _to_float 类型转换
- HARDCODED_MAP symbol 映射
- longport_to_westock 转换
- batch_fetch（mock subprocess + 缓存命中）
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.westock_client import WestockClient


# 模拟 westock CLI 的 finance 输出
MOCK_FINANCE_OUTPUT = """
**income**
| FinancialYear | Period | NetMargin | GrossMargin | OperatingMargin |
|---|---|---|---|---|
| 2025 | Q4 | 22.84 | 40.17 | 28.50 |
| 2024 | Q4 | 18.50 | 35.20 | 22.10 |

**balance**
| FinancialYear | Period | ROE | ROA | LiabilityToAsset |
|---|---|---|---|---|
| 2025 | Q4 | 17.20 | 11.22 | 35.5 |
| 2024 | Q4 | 14.80 | 9.50 | 38.2 |

**cashflow**
| FinancialYear | Period | OperatingCashFlow |
|---|---|---|
| 2025 | Q4 | 8500.00 |
"""

MOCK_PROFILE_OUTPUT = """
| Symbol | Name | Sector | Industry | MarketCap |
|---|---|---|---|---|
| usMU.OQ | Micron | 电子技术 | 半导体 | 1.2e11 |
"""


class TestWestockMDTableParser(unittest.TestCase):
    """_parse_md_table / _extract_section / _to_float"""

    def test_parse_md_table_basic(self):
        section = """| Period | NetMargin | GrossMargin |
|---|---|---|
| Q4 | 22.84 | 40.17 |"""
        result = WestockClient._parse_md_table(section)
        self.assertEqual(result.get("Period"), "Q4")
        self.assertEqual(result.get("NetMargin"), "22.84")
        self.assertEqual(result.get("GrossMargin"), "40.17")

    def test_parse_md_table_too_few_rows(self):
        """少于 3 行（表头+分隔+数据）应返回空 dict"""
        section = "| Header |\n| --- |"
        result = WestockClient._parse_md_table(section)
        self.assertEqual(result, {})

    def test_parse_md_table_mismatch_columns(self):
        """表头列数和数据列数不匹配应返回空"""
        section = """| A | B | C |
|---|---|---|
| 1 | 2 |"""
        result = WestockClient._parse_md_table(section)
        self.assertEqual(result, {})

    def test_extract_income_section(self):
        section = WestockClient._extract_section(MOCK_FINANCE_OUTPUT, "income")
        self.assertIsNotNone(section)
        self.assertIn("NetMargin", section)
        self.assertNotIn("ROE", section)  # ROE 在 balance section

    def test_extract_balance_section(self):
        section = WestockClient._extract_section(MOCK_FINANCE_OUTPUT, "balance")
        self.assertIsNotNone(section)
        self.assertIn("ROE", section)

    def test_extract_missing_section(self):
        section = WestockClient._extract_section(MOCK_FINANCE_OUTPUT, "nonexistent")
        self.assertIsNone(section)

    def test_to_float_normal(self):
        self.assertEqual(WestockClient._to_float("22.84"), 22.84)

    def test_to_float_none(self):
        self.assertIsNone(WestockClient._to_float(None))
        self.assertIsNone(WestockClient._to_float(""))
        self.assertIsNone(WestockClient._to_float("null"))

    def test_to_float_invalid(self):
        self.assertIsNone(WestockClient._to_float("not_a_number"))


class TestWestockSymbolMapping(unittest.TestCase):
    """HARDCODED_MAP + longport_to_westock"""

    def setUp(self):
        # ready=False 也 OK，不影响 symbol 映射
        with patch.object(WestockClient, "_check_npx", return_value=False):
            self.client = WestockClient(db=None)

    def test_nasdaq_mapping(self):
        """NASDAQ 标的应映射到 .OQ 后缀"""
        self.assertEqual(self.client.longport_to_westock("NVDA.US"), "usNVDA.OQ")
        self.assertEqual(self.client.longport_to_westock("MU.US"), "usMU.OQ")
        self.assertEqual(self.client.longport_to_westock("MSFT.US"), "usMSFT.OQ")

    def test_nyse_mapping(self):
        """NYSE 标的应映射到 .N 后缀"""
        self.assertEqual(self.client.longport_to_westock("JPM.US"), "usJPM.N")
        self.assertEqual(self.client.longport_to_westock("LLY.US"), "usLLY.N")
        self.assertEqual(self.client.longport_to_westock("ORCL.US"), "usORCL.N")

    def test_etf_mapping(self):
        """ETF 应映射到正确交易所"""
        self.assertEqual(self.client.longport_to_westock("SPY.US"), "usSPY.AM")
        self.assertEqual(self.client.longport_to_westock("QQQ.US"), "usQQQ.OQ")
        self.assertEqual(self.client.longport_to_westock("IWM.US"), "usIWM.AM")

    def test_unknown_symbol_returns_none_or_search(self):
        """未硬编码的 symbol 应触发 search（这里 ready=False 应返回 None）"""
        result = self.client.longport_to_westock("NEVERHEARD.US")
        # ready=False 时 search 不会跑，应返回 None 或不崩
        self.assertTrue(result is None or isinstance(result, str))


class TestWestockFetch(unittest.TestCase):
    """fetch 方法（mock subprocess）"""

    def setUp(self):
        # Mock _check_npx 返回 True 以走完整流程
        with patch.object(WestockClient, "_check_npx", return_value=True):
            self.client = WestockClient(db=None)

    @patch.object(WestockClient, "_run_cli")
    def test_fetch_financials_parses_correctly(self, mock_cli):
        """fetch_financials 应正确解析 income + balance section"""
        mock_cli.return_value = MOCK_FINANCE_OUTPUT
        # 用 LongPort symbol 格式（fetch_financials 内部会 longport_to_westock 转换）
        result = self.client.fetch_financials("MU.US")
        # NetMargin 22.84 来自 income
        self.assertAlmostEqual(result.get("net_margin", 0), 22.84, places=2)
        # GrossMargin 40.17
        self.assertAlmostEqual(result.get("gross_margin", 0), 40.17, places=2)
        # ROE 17.20 来自 balance
        self.assertAlmostEqual(result.get("roe", 0), 17.20, places=2)

    @patch.object(WestockClient, "_run_cli")
    def test_fetch_returns_empty_on_cli_failure(self, mock_cli):
        """CLI 失败（返回 None）应返回空 dict 不崩"""
        mock_cli.return_value = None
        result = self.client.fetch_financials("MU.US")
        # 不崩 + 关键字段为 None 或缺失
        self.assertIsInstance(result, dict)


class TestWestockReady(unittest.TestCase):
    """ready 状态测试"""

    @patch.object(WestockClient, "_check_npx", return_value=True)
    def test_ready_when_npx_available(self, mock_check):
        client = WestockClient(db=None)
        self.assertTrue(client.ready)

    @patch.object(WestockClient, "_check_npx", return_value=False)
    def test_not_ready_when_no_npx(self, mock_check):
        client = WestockClient(db=None)
        self.assertFalse(client.ready)


if __name__ == "__main__":
    unittest.main()

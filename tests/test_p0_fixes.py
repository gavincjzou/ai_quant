"""阶段 11 P0 修复测试（dev-13）

P0-1: PaperTradingOrchestrator._refresh_position_prices
P0-2: LongPortClient.connect_quote 独立重试

全 mock，不依赖真实 LongPort / DB。
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# P0-1：_refresh_position_prices 测试
# ============================================================

class TestRefreshPositionPrices(unittest.TestCase):
    """daily-scan 刷持仓现价"""

    def _make_orch_mock(self, positions: dict, fetch_result: dict, fetch_raises=None):
        """构造一个最小可用的 orchestrator 桩，只挂必要属性。"""
        from scripts.run_paper_trade import PaperTradingOrchestrator

        # 不调真实 __init__（会建 DB / 拉 config / 连 LongPort）
        orch = PaperTradingOrchestrator.__new__(PaperTradingOrchestrator)

        # mock trader
        orch.trader = MagicMock()
        orch.trader.positions = positions
        orch.trader.update_prices = MagicMock()

        # mock fetcher
        orch.fetcher = MagicMock()
        if fetch_raises is not None:
            orch.fetcher.fetch_history = MagicMock(side_effect=fetch_raises)
        else:
            orch.fetcher.fetch_history = MagicMock(return_value=fetch_result)

        return orch

    def _kline_df(self, last_close: float):
        return pd.DataFrame({
            "date": ["2026-04-25", "2026-04-28", "2026-04-29"],
            "open":  [100.0, 101.0, 102.0],
            "high":  [105.0, 106.0, 107.0],
            "low":   [99.0,  100.0, 101.0],
            "close": [104.0, 103.0, last_close],
            "volume": [1000, 1100, 1200],
        })

    def test_no_positions_skip(self):
        """空持仓 → 直接跳过，不调 fetcher"""
        orch = self._make_orch_mock(positions={}, fetch_result={})
        result = orch._refresh_position_prices()
        self.assertEqual(result["ok"], [])
        self.assertEqual(result["fail"], [])
        orch.fetcher.fetch_history.assert_not_called()
        orch.trader.update_prices.assert_not_called()

    def test_all_success(self):
        """3 只持仓全部拉到新价 → update_prices 被正确调用"""
        positions = {
            "MSFT.US": {"quantity": 100, "avg_cost": 510.0, "current_price": 510.0},
            "NVDA.US": {"quantity": 200, "avg_cost": 201.0, "current_price": 201.0},
            "TSM.US":  {"quantity": 100, "avg_cost": 386.5, "current_price": 386.5},
        }
        fetch_result = {
            "MSFT.US": self._kline_df(506.32),
            "NVDA.US": self._kline_df(209.25),
            "TSM.US":  self._kline_df(393.83),
        }
        orch = self._make_orch_mock(positions=positions, fetch_result=fetch_result)

        result = orch._refresh_position_prices()

        self.assertEqual(set(result["ok"]), {"MSFT.US", "NVDA.US", "TSM.US"})
        self.assertEqual(result["fail"], [])
        self.assertEqual(result["prices"]["MSFT.US"], 506.32)
        self.assertEqual(result["prices"]["NVDA.US"], 209.25)
        self.assertEqual(result["prices"]["TSM.US"], 393.83)
        orch.trader.update_prices.assert_called_once_with({
            "MSFT.US": 506.32,
            "NVDA.US": 209.25,
            "TSM.US": 393.83,
        })

    def test_partial_failure(self):
        """部分失败：fetcher 没返回某只 → 该只算 fail，其他正常更新"""
        positions = {
            "MSFT.US": {"quantity": 100, "avg_cost": 510.0, "current_price": 510.0},
            "NVDA.US": {"quantity": 200, "avg_cost": 201.0, "current_price": 201.0},
        }
        fetch_result = {
            "MSFT.US": self._kline_df(506.32),
            # NVDA 没返回
        }
        orch = self._make_orch_mock(positions=positions, fetch_result=fetch_result)

        result = orch._refresh_position_prices()

        self.assertEqual(result["ok"], ["MSFT.US"])
        self.assertEqual(result["fail"], ["NVDA.US"])
        # update_prices 只用部分价格调一次
        orch.trader.update_prices.assert_called_once_with({"MSFT.US": 506.32})

    def test_empty_df(self):
        """fetcher 返回空 DataFrame → 算 fail，不污染 prices"""
        positions = {"AVGO.US": {"quantity": 100, "avg_cost": 290.0, "current_price": 290.0}}
        fetch_result = {"AVGO.US": pd.DataFrame()}
        orch = self._make_orch_mock(positions=positions, fetch_result=fetch_result)

        result = orch._refresh_position_prices()

        self.assertEqual(result["ok"], [])
        self.assertEqual(result["fail"], ["AVGO.US"])
        orch.trader.update_prices.assert_not_called()

    def test_invalid_close(self):
        """close 为 0 或负数 → 算 fail（防数据异常）"""
        positions = {"AVGO.US": {"quantity": 100, "avg_cost": 290.0, "current_price": 290.0}}
        fetch_result = {"AVGO.US": self._kline_df(-1.0)}
        orch = self._make_orch_mock(positions=positions, fetch_result=fetch_result)

        result = orch._refresh_position_prices()

        self.assertEqual(result["fail"], ["AVGO.US"])
        orch.trader.update_prices.assert_not_called()

    def test_fetcher_raises(self):
        """整体 fetch_history 抛异常 → 全部算 fail，不抛异常出来"""
        positions = {
            "MSFT.US": {"quantity": 100, "avg_cost": 510.0, "current_price": 510.0},
            "NVDA.US": {"quantity": 200, "avg_cost": 201.0, "current_price": 201.0},
        }
        orch = self._make_orch_mock(
            positions=positions,
            fetch_result={},
            fetch_raises=RuntimeError("LongPort completely down"),
        )

        # 关键：不应该抛异常出来（守护 daily-scan 主流程）
        result = orch._refresh_position_prices()

        self.assertEqual(result["ok"], [])
        self.assertEqual(set(result["fail"]), {"MSFT.US", "NVDA.US"})
        orch.trader.update_prices.assert_not_called()


# ============================================================
# P0-2：connect_quote 独立重试测试
# ============================================================

class TestConnectQuoteRetry(unittest.TestCase):
    """LongPort QuoteContext 偶发失败时的重试"""

    def _make_client(self, retry_max=3, retry_delay=0):
        """构造 client，跳过 SDK 真实初始化，直接 patch 内部状态"""
        from src.data import longport_client as lc_module

        # 用 __new__ 跳过 __init__ 防止真连 LongPort
        client = lc_module.LongPortClient.__new__(lc_module.LongPortClient)
        client._quote_ctx = None
        client._trade_ctx = None
        client._config = {
            "connect_retry_max": retry_max,
            "connect_retry_delay": retry_delay,
        }
        client._last_request_time = 0
        client._throttle_interval = 0
        return client

    def test_first_attempt_success(self):
        """第一次就成功 → 不重试"""
        client = self._make_client(retry_max=5)
        fake_ctx = MagicMock(name="QuoteContext")

        with patch("src.data.longport_client.Config") as mock_cfg, \
             patch("src.data.longport_client.QuoteContext", return_value=fake_ctx) as mock_qc:
            mock_cfg.from_env.return_value = MagicMock()
            ctx = client.connect_quote()

        self.assertIs(ctx, fake_ctx)
        # QuoteContext 只被调一次
        self.assertEqual(mock_qc.call_count, 1)

    def test_retry_then_success(self):
        """前 2 次 socket/token 失败 → 第 3 次成功"""
        client = self._make_client(retry_max=5, retry_delay=0)
        fake_ctx = MagicMock(name="QuoteContext")

        socket_err = Exception(
            "error sending request for url "
            "(https://openapi.longportapp.com/v1/socket/token): "
            "client error (Connect)"
        )

        # 前 2 次抛，第 3 次返回
        side_effects = [socket_err, socket_err, fake_ctx]

        with patch("src.data.longport_client.Config") as mock_cfg, \
             patch("src.data.longport_client.QuoteContext", side_effect=side_effects) as mock_qc:
            mock_cfg.from_env.return_value = MagicMock()
            ctx = client.connect_quote()

        self.assertIs(ctx, fake_ctx)
        self.assertEqual(mock_qc.call_count, 3)

    def test_all_attempts_fail_raises(self):
        """5 次全失败 → 抛最后一个异常"""
        client = self._make_client(retry_max=3, retry_delay=0)
        socket_err = Exception("socket/token Connect error")

        with patch("src.data.longport_client.Config") as mock_cfg, \
             patch("src.data.longport_client.QuoteContext", side_effect=[socket_err] * 3) as mock_qc:
            mock_cfg.from_env.return_value = MagicMock()
            with self.assertRaises(Exception) as ctx:
                client.connect_quote()

        self.assertIn("socket/token", str(ctx.exception))
        self.assertEqual(mock_qc.call_count, 3)

    def test_cached_after_success(self):
        """连接成功后 _quote_ctx 缓存，下次直接返回不重连"""
        client = self._make_client(retry_max=5)
        fake_ctx = MagicMock(name="QuoteContext")

        with patch("src.data.longport_client.Config") as mock_cfg, \
             patch("src.data.longport_client.QuoteContext", return_value=fake_ctx) as mock_qc:
            mock_cfg.from_env.return_value = MagicMock()
            ctx1 = client.connect_quote()
            ctx2 = client.connect_quote()
            ctx3 = client.connect_quote()

        self.assertIs(ctx1, fake_ctx)
        self.assertIs(ctx2, fake_ctx)
        self.assertIs(ctx3, fake_ctx)
        # 只构造一次（后续命中 self._quote_ctx 缓存）
        self.assertEqual(mock_qc.call_count, 1)


if __name__ == "__main__":
    unittest.main()

"""阶段 11 P1 修复测试（dev-13）

P1-1: _check_data_staleness（perf/kline/factor 三类陈旧检查）
P1-2: V1 失败时若有历史 snapshot 允许下游周报跑（v1_can_run_downstream）
P1-3: replay_dates 补跑历史 daily_perf

全 mock，不依赖真实 LongPort / DB。
"""
import os
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_orch_mock(positions=None, risk_cfg=None):
    """构造最小可用 orchestrator 桩"""
    from scripts.run_paper_trade import PaperTradingOrchestrator
    orch = PaperTradingOrchestrator.__new__(PaperTradingOrchestrator)
    orch.trader = MagicMock()
    orch.trader.positions = positions or {}
    orch.trader.cash = 400000.0
    orch.trader.update_prices = MagicMock()
    orch.trader.take_daily_snapshot = MagicMock()
    orch.trader.save_state = MagicMock()
    orch.fetcher = MagicMock()
    orch.alerter = MagicMock()
    orch.calendar = MagicMock()
    orch.calendar.is_trading_day = MagicMock(return_value=True)
    orch.db = MagicMock()
    orch.risk_cfg = risk_cfg or {}
    return orch


def _mock_db_conn(orch, fetchall_results=None, fetchone_results=None):
    """让 orch.db._get_conn() 上下文管理器返回 mock"""
    mock_conn = MagicMock()
    if fetchall_results is not None:
        mock_conn.execute.return_value.fetchall.return_value = fetchall_results
    if fetchone_results is not None:
        # fetchone_results 可以是 list（多次调用）或单个值
        if isinstance(fetchone_results, list):
            mock_conn.execute.return_value.fetchone.side_effect = fetchone_results
        else:
            mock_conn.execute.return_value.fetchone.return_value = fetchone_results
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_conn)
    cm.__exit__ = MagicMock(return_value=False)
    orch.db._get_conn = MagicMock(return_value=cm)
    return mock_conn


# ============================================================
# P1-1: _check_data_staleness
# ============================================================

class TestCheckDataStaleness(unittest.TestCase):

    def test_no_issues_when_data_fresh(self):
        """所有数据新鲜 → 不告警"""
        orch = _make_orch_mock(positions={"MSFT.US": {"quantity": 100}})
        today = date.today()
        # market_value 有变化
        perf_rows = [
            (today.isoformat(), 100000.0),
            ((today - timedelta(days=1)).isoformat(), 99500.0),
            ((today - timedelta(days=2)).isoformat(), 100200.0),
        ]
        # k线日期 = 今天
        kline_rows = [("MSFT.US", f"{today.isoformat()} 12:00:00")]
        # factor v1 = 今天
        factor_row = (today.isoformat(),)

        # 三次 _get_conn 调用按顺序返回不同结果
        mock_conn = MagicMock()
        responses = [
            MagicMock(fetchall=MagicMock(return_value=perf_rows)),
            MagicMock(fetchall=MagicMock(return_value=kline_rows)),
            MagicMock(fetchone=MagicMock(return_value=factor_row)),
        ]
        mock_conn.execute.side_effect = responses
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_conn)
        cm.__exit__ = MagicMock(return_value=False)
        orch.db._get_conn = MagicMock(return_value=cm)

        result = orch._check_data_staleness()
        self.assertEqual(result["issues"], [])
        orch.alerter.warning.assert_not_called()

    def test_perf_stale_triggers_alert(self):
        """daily_perf 连续 N 天 market_value 完全相同 → 告警"""
        orch = _make_orch_mock(positions={})
        today = date.today()
        # 3 天 market_value 完全相同
        perf_rows = [
            (today.isoformat(), 400000.0),
            ((today - timedelta(days=1)).isoformat(), 400000.0),
            ((today - timedelta(days=2)).isoformat(), 400000.0),
        ]
        # 无持仓 → kline 检查不会触发
        # factor v1 今天 → 不告警
        factor_row = (today.isoformat(),)

        mock_conn = MagicMock()
        responses = [
            MagicMock(fetchall=MagicMock(return_value=perf_rows)),
            MagicMock(fetchone=MagicMock(return_value=factor_row)),
        ]
        mock_conn.execute.side_effect = responses
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_conn)
        cm.__exit__ = MagicMock(return_value=False)
        orch.db._get_conn = MagicMock(return_value=cm)

        result = orch._check_data_staleness()
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("market_value", result["issues"][0])
        orch.alerter.warning.assert_called_once()
        # 告警标题应该是数据陈旧度
        call_kwargs = orch.alerter.warning.call_args.kwargs
        self.assertIn("陈旧", call_kwargs["title"])

    def test_kline_stale_triggers_alert(self):
        """持仓 K 线日期超过阈值 → 告警"""
        orch = _make_orch_mock(positions={"MSFT.US": {"quantity": 100}})
        today = date.today()
        old_date = (today - timedelta(days=10)).isoformat()
        # perf 正常波动
        perf_rows = [
            (today.isoformat(), 100000.0),
            ((today - timedelta(days=1)).isoformat(), 99000.0),
            ((today - timedelta(days=2)).isoformat(), 101000.0),
        ]
        kline_rows = [("MSFT.US", f"{old_date} 12:00:00")]
        factor_row = (today.isoformat(),)

        mock_conn = MagicMock()
        responses = [
            MagicMock(fetchall=MagicMock(return_value=perf_rows)),
            MagicMock(fetchall=MagicMock(return_value=kline_rows)),
            MagicMock(fetchone=MagicMock(return_value=factor_row)),
        ]
        mock_conn.execute.side_effect = responses
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_conn)
        cm.__exit__ = MagicMock(return_value=False)
        orch.db._get_conn = MagicMock(return_value=cm)

        result = orch._check_data_staleness()
        self.assertTrue(any("kline_data" in i for i in result["issues"]))
        orch.alerter.warning.assert_called_once()

    def test_factor_no_data_triggers_alert(self):
        """factor_snapshots V1 完全无数据 → 告警"""
        orch = _make_orch_mock(positions={})
        today = date.today()
        perf_rows = [
            (today.isoformat(), 400000.0),
            ((today - timedelta(days=1)).isoformat(), 400500.0),
            ((today - timedelta(days=2)).isoformat(), 401000.0),
        ]
        factor_row = (None,)

        mock_conn = MagicMock()
        responses = [
            MagicMock(fetchall=MagicMock(return_value=perf_rows)),
            MagicMock(fetchone=MagicMock(return_value=factor_row)),
        ]
        mock_conn.execute.side_effect = responses
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_conn)
        cm.__exit__ = MagicMock(return_value=False)
        orch.db._get_conn = MagicMock(return_value=cm)

        result = orch._check_data_staleness()
        self.assertTrue(any("factor_snapshots V1 完全无数据" in i for i in result["issues"]))
        orch.alerter.warning.assert_called_once()


# ============================================================
# P1-3: replay_dates
# ============================================================

class TestReplayDates(unittest.TestCase):

    def _kline_df_with_dates(self, dates_closes):
        """构造含指定日期的 K 线 df"""
        return pd.DataFrame({
            "date": [f"{d} 12:00:00" for d, _ in dates_closes],
            "open":  [c - 1 for _, c in dates_closes],
            "high":  [c + 2 for _, c in dates_closes],
            "low":   [c - 2 for _, c in dates_closes],
            "close": [c for _, c in dates_closes],
            "volume": [1000] * len(dates_closes),
        })

    def test_invalid_date_format_raises(self):
        """from_date 格式错误 → ValueError"""
        orch = _make_orch_mock(positions={"MSFT.US": {"quantity": 100}})
        with self.assertRaises(ValueError):
            orch.replay_dates(from_date="2026/04/23")

    def test_to_before_from_raises(self):
        """to_date < from_date → ValueError"""
        orch = _make_orch_mock(positions={"MSFT.US": {"quantity": 100}})
        with self.assertRaises(ValueError):
            orch.replay_dates(from_date="2026-04-23", to_date="2026-04-20")

    def test_no_positions_returns_empty(self):
        """无持仓 → 直接返回，不调 fetcher"""
        orch = _make_orch_mock(positions={})
        result = orch.replay_dates(from_date="2026-04-23", to_date="2026-04-25", dry_run=True)
        self.assertEqual(result["processed"], [])
        orch.fetcher.fetch_history.assert_not_called()

    def test_dry_run_no_writes(self):
        """dry_run=True 时不调 update_prices / take_daily_snapshot / save_state"""
        positions = {"MSFT.US": {"quantity": 100}}
        orch = _make_orch_mock(positions=positions)
        # K 线含 04-23/04-24
        orch.fetcher.fetch_history.return_value = {
            "MSFT.US": self._kline_df_with_dates([
                ("2026-04-23", 510.0),
                ("2026-04-24", 515.0),
            ])
        }
        # 模拟 db 查 daily_perf 旧值
        _mock_db_conn(orch, fetchone_results=[(799094.0, 79865.0, 0.0), (799094.0, 79865.0, 0.0)])

        result = orch.replay_dates(from_date="2026-04-23", to_date="2026-04-24", dry_run=True)

        self.assertEqual(len(result["processed"]), 2)
        self.assertTrue(result["dry_run"])
        # 关键：dry_run 不能调任何写方法
        orch.trader.update_prices.assert_not_called()
        orch.trader.take_daily_snapshot.assert_not_called()
        orch.trader.save_state.assert_not_called()
        # 但 fetch_history 必须调（save_to_db=False）
        orch.fetcher.fetch_history.assert_called_once()
        call_kwargs = orch.fetcher.fetch_history.call_args.kwargs
        self.assertFalse(call_kwargs["save_to_db"])

    def test_real_run_writes_and_saves(self):
        """非 dry_run → 调 update_prices / take_daily_snapshot / save_state"""
        positions = {"MSFT.US": {"quantity": 100}}
        orch = _make_orch_mock(positions=positions)
        orch.fetcher.fetch_history.return_value = {
            "MSFT.US": self._kline_df_with_dates([
                ("2026-04-23", 510.0),
            ])
        }
        _mock_db_conn(orch, fetchone_results=[(799094.0, 79865.0, 0.0)])

        result = orch.replay_dates(from_date="2026-04-23", to_date="2026-04-23", dry_run=False)

        self.assertEqual(len(result["processed"]), 1)
        orch.trader.update_prices.assert_called_once_with({"MSFT.US": 510.0})
        orch.trader.take_daily_snapshot.assert_called_once_with(scan_date="2026-04-23")
        orch.trader.save_state.assert_called_once()

    def test_skip_non_trading_day(self):
        """周末非交易日跳过"""
        positions = {"MSFT.US": {"quantity": 100}}
        orch = _make_orch_mock(positions=positions)
        # 模拟：04-25 是周六，is_trading_day 返回 False
        def is_td(d):
            return d.weekday() < 5
        orch.calendar.is_trading_day = MagicMock(side_effect=is_td)

        orch.fetcher.fetch_history.return_value = {
            "MSFT.US": self._kline_df_with_dates([
                ("2026-04-24", 515.0),
                ("2026-04-27", 520.0),
            ])
        }
        _mock_db_conn(orch, fetchone_results=[(800000.0, 80000.0, 0.0), (800000.0, 80000.0, 0.0)])

        result = orch.replay_dates(from_date="2026-04-24", to_date="2026-04-27", dry_run=True)
        # 04-24 周五 + 04-27 周一 = 2 个交易日（25、26 是周末跳过）
        self.assertEqual(len(result["processed"]), 2)
        processed_dates = [e["date"] for e in result["processed"]]
        self.assertIn("2026-04-24", processed_dates)
        self.assertIn("2026-04-27", processed_dates)

    def test_missing_kline_uses_fallback(self):
        """某只持仓某日无 K 线 → 用最近一个交易日的 close"""
        positions = {
            "MSFT.US": {"quantity": 100},
            "AVGO.US": {"quantity": 100},
        }
        orch = _make_orch_mock(positions=positions)
        # MSFT 04-23 + 04-24 都有；AVGO 只有 04-23
        orch.fetcher.fetch_history.return_value = {
            "MSFT.US": self._kline_df_with_dates([
                ("2026-04-23", 510.0),
                ("2026-04-24", 515.0),
            ]),
            "AVGO.US": self._kline_df_with_dates([
                ("2026-04-23", 290.0),
                # 04-24 缺
            ]),
        }
        _mock_db_conn(orch, fetchone_results=[(800000.0, 80000.0, 0.0), (800000.0, 80000.0, 0.0)])

        result = orch.replay_dates(from_date="2026-04-23", to_date="2026-04-24", dry_run=True)
        # 04-24 那天 AVGO 用 04-23 的 close fallback，仍然有 missing 标记
        e_24 = next(e for e in result["processed"] if e["date"] == "2026-04-24")
        self.assertTrue(any("AVGO.US" in m for m in e_24["missing"]))


# ============================================================
# P1-2: V1 失败时允许下游用历史 snapshot
# ============================================================
# P1-2 的逻辑分散在 daily-scan main 流程里，整体集成测试难写
# 这里用一个 smoke test 验证 v1_can_run_downstream 的 SQL 查询模式正确
class TestV1FallbackQuery(unittest.TestCase):

    def test_history_v1_query_returns_max_date(self):
        """SQL 查询模式应返回 MAX(date) 作为 fallback 日期"""
        # 直接验证 SQL 字符串就行
        sql = "SELECT MAX(date) FROM factor_snapshots WHERE version='v1'"
        # 这里只确保 SQL 没改变（避免重构时漏改）
        with open(
            os.path.join(PROJECT_ROOT, "scripts", "run_paper_trade.py"),
            encoding="utf-8",
        ) as f:
            content = f.read()
        self.assertIn(sql, content)
        # v1_can_run_downstream 变量名
        self.assertIn("v1_can_run_downstream", content)


if __name__ == "__main__":
    unittest.main()

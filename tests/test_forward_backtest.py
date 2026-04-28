"""阶段 11 P1-5：forward_backtest_factor.py 测试

不依赖真实 LongPort/Westock，全部 mock SQL 返回。
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import forward_backtest_factor as fb


class TestAggregate(unittest.TestCase):
    """汇总函数测试"""

    def test_empty_results(self):
        s = fb.aggregate_forward_results([])
        self.assertEqual(s["n_snapshots"], 0)
        self.assertEqual(s["avg_alpha"], 0)
        self.assertIsNone(s["avg_ic"])

    def test_aggregate_basic(self):
        results = [
            {"snapshot_date": "2026-01-01", "n_days": 20, "n_symbols": 5,
             "top_symbols": ["A", "B"], "port_total_return": 0.10,
             "bench_total_return": 0.05, "alpha": 0.04, "beta": 1.0,
             "sharpe": 1.5, "max_drawdown": 0.05, "win_rate": 0.55, "ic": 0.06},
            {"snapshot_date": "2026-01-08", "n_days": 20, "n_symbols": 5,
             "top_symbols": ["A", "B"], "port_total_return": 0.05,
             "bench_total_return": 0.08, "alpha": -0.03, "beta": 1.1,
             "sharpe": 0.8, "max_drawdown": 0.07, "win_rate": 0.45, "ic": -0.02},
        ]
        s = fb.aggregate_forward_results(results)
        self.assertEqual(s["n_snapshots"], 2)
        self.assertAlmostEqual(s["avg_alpha"], 0.005, places=4)  # (0.04-0.03)/2
        self.assertAlmostEqual(s["avg_sharpe"], 1.15, places=3)
        self.assertAlmostEqual(s["win_rate_vs_bench"], 0.5)  # 1/2 跑赢
        self.assertAlmostEqual(s["avg_ic"], 0.02, places=4)
        self.assertAlmostEqual(s["ic_positive_rate"], 0.5)

    def test_aggregate_with_none_ic(self):
        """部分 snapshot 没 IC 时应忽略 None"""
        results = [
            {"snapshot_date": "2026-01-01", "n_days": 20, "n_symbols": 5,
             "top_symbols": ["A"], "port_total_return": 0.10,
             "bench_total_return": 0.05, "alpha": 0.04, "beta": 1.0,
             "sharpe": 1.5, "max_drawdown": 0.05, "win_rate": 0.55, "ic": 0.06},
            {"snapshot_date": "2026-01-08", "n_days": 20, "n_symbols": 5,
             "top_symbols": ["A"], "port_total_return": 0.05,
             "bench_total_return": 0.08, "alpha": -0.03, "beta": 1.1,
             "sharpe": 0.8, "max_drawdown": 0.07, "win_rate": 0.45, "ic": None},
        ]
        s = fb.aggregate_forward_results(results)
        self.assertEqual(s["n_snapshots"], 2)
        self.assertEqual(s["n_with_ic"], 1)
        self.assertAlmostEqual(s["avg_ic"], 0.06, places=4)


class TestRenderReport(unittest.TestCase):
    """报告渲染测试"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_render_empty_results(self):
        """无数据应渲染 error callout"""
        path = os.path.join(self.tmpdir, "empty.md")
        fb.render_report("v1", 5, 30, "QQQ.US", [], {"n_snapshots": 0}, path)
        with open(path) as f:
            content = f.read()
        self.assertIn("无可用数据", content)
        self.assertIn("[!error]", content)

    def test_render_small_sample_warning(self):
        """样本 <30 应有警告"""
        results = [{
            "snapshot_date": "2026-01-01", "n_days": 20, "n_symbols": 5,
            "top_symbols": ["A", "B", "C"], "port_total_return": 0.10,
            "bench_total_return": 0.05, "alpha": 0.04, "beta": 1.0,
            "sharpe": 1.5, "max_drawdown": 0.05, "win_rate": 0.55, "ic": 0.06,
        }]
        summary = fb.aggregate_forward_results(results)
        path = os.path.join(self.tmpdir, "small.md")
        fb.render_report("v1", 5, 30, "QQQ.US", results, summary, path)
        with open(path) as f:
            content = f.read()
        self.assertIn("样本仅 1 个", content)
        self.assertIn("[!warning]", content)
        # IC 解读
        self.assertIn("IC = +0.060", content)
        self.assertIn("因子信号有效", content)

    def test_render_includes_metrics(self):
        results = [
            {"snapshot_date": "2026-01-01", "n_days": 20, "n_symbols": 5,
             "top_symbols": ["A"], "port_total_return": 0.10,
             "bench_total_return": 0.05, "alpha": 0.04, "beta": 1.0,
             "sharpe": 1.5, "max_drawdown": 0.05, "win_rate": 0.55, "ic": 0.06},
        ]
        summary = fb.aggregate_forward_results(results)
        path = os.path.join(self.tmpdir, "ok.md")
        fb.render_report("v1", 5, 30, "QQQ.US", results, summary, path)
        with open(path) as f:
            content = f.read()
        self.assertIn("汇总指标", content)
        self.assertIn("各 Snapshot 明细", content)
        self.assertIn("2026-01-01", content)


class TestIcInterpretation(unittest.TestCase):
    """IC 不同区间应给出不同解读"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _render_with_ic(self, ic_val):
        results = [{
            "snapshot_date": "2026-01-01", "n_days": 20, "n_symbols": 5,
            "top_symbols": ["A"], "port_total_return": 0.05,
            "bench_total_return": 0.05, "alpha": 0.0, "beta": 1.0,
            "sharpe": 1.0, "max_drawdown": 0.05, "win_rate": 0.5, "ic": ic_val,
        }]
        summary = fb.aggregate_forward_results(results)
        path = os.path.join(self.tmpdir, f"ic_{ic_val}.md")
        fb.render_report("v1", 5, 30, "QQQ.US", results, summary, path)
        with open(path) as f:
            return f.read()

    def test_strong_positive_ic(self):
        c = self._render_with_ic(0.10)
        self.assertIn("因子信号有效", c)

    def test_weak_positive_ic(self):
        c = self._render_with_ic(0.03)
        self.assertIn("微弱信号", c)

    def test_random_ic(self):
        c = self._render_with_ic(0.0)
        self.assertIn("无明显信号", c)

    def test_negative_ic(self):
        c = self._render_with_ic(-0.10)
        self.assertIn("方向反了", c)


class TestForwardOneSnapshot(unittest.TestCase):
    """单 snapshot forward 测算（mock 数据库）"""

    @patch("scripts.forward_backtest_factor.compute_portfolio_returns")
    @patch("scripts.forward_backtest_factor.load_kline_for_period")
    @patch("scripts.forward_backtest_factor.get_topn_with_scores")
    @patch("scripts.forward_backtest_factor._compute_ic")
    def test_forward_returns_metrics(self, mock_ic, mock_topn, mock_kline, mock_port):
        # mock Top-3
        mock_topn.return_value = [
            {"symbol": "A", "total_score": 1.5, "rank": 1},
            {"symbol": "B", "total_score": 1.0, "rank": 2},
            {"symbol": "C", "total_score": 0.5, "rank": 3},
        ]
        # mock 组合收益（20 个交易日）
        idx = pd.date_range("2026-01-01", periods=20, freq="B")
        mock_port.return_value = (pd.Series([0.005] * 20, index=idx), {})
        # mock 基准 K 线
        mock_kline.return_value = pd.DataFrame({
            "date": idx, "close": [100 + i * 0.5 for i in range(20)],
        })
        mock_ic.return_value = 0.08

        db = MagicMock()
        result = fb.forward_one_snapshot(db, "v1", "2026-01-01", top=3, forward_days=20)
        self.assertIsNotNone(result)
        self.assertEqual(result["snapshot_date"], "2026-01-01")
        self.assertEqual(result["n_symbols"], 3)
        self.assertEqual(result["ic"], 0.08)
        self.assertGreater(result["port_total_return"], 0)

    @patch("scripts.forward_backtest_factor.compute_portfolio_returns")
    @patch("scripts.forward_backtest_factor.get_topn_with_scores")
    def test_forward_returns_none_when_no_data(self, mock_topn, mock_port):
        """组合收益为空应返回 None"""
        mock_topn.return_value = [{"symbol": "A", "total_score": 1.5, "rank": 1}]
        mock_port.return_value = (pd.Series(dtype="float64"), {})

        db = MagicMock()
        result = fb.forward_one_snapshot(db, "v1", "2026-01-01", top=1, forward_days=20)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

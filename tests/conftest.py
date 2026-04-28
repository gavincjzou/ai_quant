"""
tests/conftest.py - 公共测试 fixture + helper

虽然项目用 unittest 风格（不是 pytest 强依赖），但本文件提供：
1. 标准化的 sys.path 注入（避免每个 test 文件都重写）
2. 公共 helper 函数：sample_kline / sample_factor_snapshot / sample_positions
3. in-memory SQLite 工厂（避免污染 data_cache/quant.db）
4. 当用 pytest 跑时，这些 helper 也可作为 fixture（@pytest.fixture 包装）
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# ============================================================
# sys.path：让所有 test 文件都能 import src.*
# ============================================================
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ============================================================
# K 线样本生成器
# ============================================================
def sample_kline(
    symbol: str = "TEST.US",
    n: int = 60,
    start_price: float = 100.0,
    trend: str = "up",
    seed: int = 42,
) -> pd.DataFrame:
    """生成可控的 K 线数据用于测试。

    Args:
        symbol: 标的（不实际使用，仅文档化）
        n: 多少根 K 线
        start_price: 起始价
        trend: "up" 上升 / "down" 下降 / "sideways" 震荡
        seed: 随机种子，保证测试可复现

    Returns:
        DataFrame: date / open / high / low / close / volume / turnover
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    if trend == "up":
        close = start_price + np.arange(n) * 0.5 + rng.standard_normal(n) * 0.3
    elif trend == "down":
        close = start_price - np.arange(n) * 0.5 + rng.standard_normal(n) * 0.3
    else:  # sideways
        close = start_price + rng.standard_normal(n) * 1.0

    close = np.maximum(close, 1.0)  # 价格不能 ≤ 0

    df = pd.DataFrame({
        "date": dates,
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": rng.integers(1_000_000, 10_000_000, size=n),
        "turnover": close * rng.integers(1_000_000, 10_000_000, size=n),
    })
    return df


# ============================================================
# Factor 快照样本生成器
# ============================================================
def sample_factor_raw_df(
    symbols: Optional[List[str]] = None,
    include_quality: bool = True,
) -> pd.DataFrame:
    """构造 V1Scorer.score_v1 的输入 DataFrame。

    覆盖 V0 七字段 + V1 quality / industry 字段。
    """
    if symbols is None:
        symbols = ["NVDA.US", "MU.US", "JPM.US", "QQQ.US", "JNJ.US"]

    rng = np.random.default_rng(42)
    n = len(symbols)

    # V0 七字段
    data = {
        "symbol": symbols,
        "pe_ttm_ratio": rng.uniform(15, 50, n),
        "pb_ratio": rng.uniform(2, 12, n),
        "dividend_ratio_ttm": rng.uniform(0, 0.04, n),
        "total_market_value": rng.uniform(1e10, 3e12, n),
        "five_day_change_rate": rng.uniform(-0.05, 0.08, n),
        "half_year_change_rate": rng.uniform(-0.2, 0.6, n),
        "turnover_rate": rng.uniform(0.005, 0.05, n),
    }

    if include_quality:
        # 中文行业（含一只半导体 + 一只 ETF + 一只银行 + 一只医药）
        industry_map = {
            "NVDA.US": "半导体",
            "MU.US": "半导体",
            "JPM.US": "大型银行",
            "QQQ.US": "ETF/指数基金",
            "JNJ.US": "大型药物生产商",
        }
        sector_map = {
            "NVDA.US": "电子技术",
            "MU.US": "电子技术",
            "JPM.US": "金融",
            "QQQ.US": "ETF",
            "JNJ.US": "医药",
        }
        data["industry"] = [industry_map.get(s, "其他") for s in symbols]
        data["sector"] = [sector_map.get(s, "其他") for s in symbols]
        data["net_margin"] = rng.uniform(5, 60, n)
        data["gross_margin"] = rng.uniform(20, 80, n)
        data["operating_margin"] = rng.uniform(10, 50, n)
        data["roe"] = rng.uniform(5, 100, n)

    return pd.DataFrame(data)


# ============================================================
# 持仓样本（trading_state.paper.positions 格式）
# ============================================================
def sample_positions(symbols: Optional[List[str]] = None) -> Dict[str, dict]:
    """模拟 trading_state.paper.positions 的格式。"""
    if symbols is None:
        symbols = ["MSFT.US", "NVDA.US", "META.US", "TSM.US", "AVGO.US"]

    return {
        sym: {
            "quantity": 100,
            "avg_cost": 150.0 + i * 20,
            "current_price": 155.0 + i * 20,
            "market_value": 100 * (155.0 + i * 20),
            "unrealized_pnl": 100 * 5.0,
        }
        for i, sym in enumerate(symbols)
    }


# ============================================================
# In-memory DB 工厂（每个 test 一个隔离的 DB）
# ============================================================
def make_inmemory_db():
    """返回一个 in-memory SQLite DatabaseManager。

    用 NamedTemporaryFile 而不是 ':memory:'，因为 DatabaseManager 用
    多次 connection（_get_conn），':memory:' 在多 connection 间不共享。
    """
    from src.data.database import DatabaseManager

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = DatabaseManager(db_path=tmp.name)
    db._tmp_path = tmp.name  # 测试结束可手动 cleanup
    return db


def cleanup_db(db) -> None:
    """清理 make_inmemory_db 创建的临时 DB 文件。"""
    if hasattr(db, "_tmp_path") and os.path.exists(db._tmp_path):
        try:
            os.unlink(db._tmp_path)
        except Exception:
            pass


# ============================================================
# pytest fixture 包装（如果用 pytest 跑可直接用）
# ============================================================
try:
    import pytest

    @pytest.fixture
    def kline():
        return sample_kline()

    @pytest.fixture
    def factor_raw_df():
        return sample_factor_raw_df()

    @pytest.fixture
    def positions():
        return sample_positions()

    @pytest.fixture
    def inmemory_db():
        db = make_inmemory_db()
        yield db
        cleanup_db(db)

except ImportError:
    pass  # 无 pytest 时静默跳过

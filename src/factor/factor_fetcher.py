"""FactorFetcher - 因子数据采集器

职责：
1. 调用 LongPort calc_indexes 批量拉取 watchlist 的实时指标
2. 把原始指标存入 SQLite factor_snapshots 表
3. 提供从 DB 读取历史快照的接口（供回测/分析用）

阶段 9 V0：仅支持 4 因子所需的 7 个原始指标。
"""
from datetime import date, datetime
from typing import List, Optional

import pandas as pd
from loguru import logger

from src.data.longport_client import LongPortClient
from src.data.database import DatabaseManager


class FactorFetcher:
    """因子原始数据采集 + 持久化"""

    # 7 个原始指标（对应 LongPort CalcIndex 枚举名）
    RAW_INDEX_NAMES = [
        "PeTtmRatio",
        "PbRatio",
        "DividendRatioTtm",
        "TotalMarketValue",
        "FiveDayChangeRate",
        "HalfYearChangeRate",
        "TurnoverRate",
    ]

    # snake_case 字段名（DB column 名，和 calc_indexes 返回字段名一致）
    RAW_FIELD_NAMES = [
        "pe_ttm_ratio",
        "pb_ratio",
        "dividend_ratio_ttm",
        "total_market_value",
        "five_day_change_rate",
        "half_year_change_rate",
        "turnover_rate",
    ]

    def __init__(self, lp_client: LongPortClient, db: DatabaseManager):
        self.lp = lp_client
        self.db = db

    def fetch(self, symbols: List[str]) -> pd.DataFrame:
        """从 LongPort 拉取原始指标（不存 DB），返回 DataFrame"""
        df = self.lp.fetch_calc_indexes(symbols, self.RAW_INDEX_NAMES)
        return df

    def fetch_and_store(
        self,
        symbols: List[str],
        snapshot_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        拉取 + 存入 factor_snapshots 表（得分字段为空，由 FactorScorer 后续填充）。

        Args:
            symbols: 标的列表
            snapshot_date: 快照日期（YYYY-MM-DD），默认今天

        Returns:
            原始指标 DataFrame
        """
        d = snapshot_date or date.today().isoformat()

        df = self.fetch(symbols)
        if df.empty:
            logger.warning(f"[FactorFetcher] 未拉到任何数据")
            return df

        # 存库（只存原始指标，得分字段留空）
        snapshots = []
        for _, row in df.iterrows():
            snap = {
                "date": d,
                "symbol": row["symbol"],
            }
            for f in self.RAW_FIELD_NAMES:
                v = row.get(f)
                # NaN → None（SQLite 存 null）
                snap[f] = None if (v is None or (isinstance(v, float) and v != v)) else float(v)
            snapshots.append(snap)

        self.db.save_factor_snapshots_batch(snapshots)
        logger.info(f"[FactorFetcher] 存储 {len(snapshots)} 条因子快照（日期={d}）")
        return df

    def load_latest(self, snapshot_date: Optional[str] = None) -> pd.DataFrame:
        """从 DB 加载最新（或指定日期）的因子快照"""
        return self.db.load_factor_snapshots(date=snapshot_date)

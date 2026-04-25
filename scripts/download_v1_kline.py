"""
阶段 9 V1 K 线下载脚本

一次性下载 V1 新增 7 只标的的 1000 天日线数据到 SQLite kline_data 表。
- ORCL / PLTR / SMCI / MU / MRVL / QCOM / INTC
- 用 LongPort 前复权 K 线
- 幂等：已存在则 skip（除非 --force）

用法：
    .venv/bin/python scripts/download_v1_kline.py            # 下载
    .venv/bin/python scripts/download_v1_kline.py --force    # 强制重下
"""
from __future__ import annotations

import argparse
import os
import sys

# 项目根
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ_ROOT)

from loguru import logger

from src.data.database import DatabaseManager
from src.data.longport_client import LongPortClient

# V1 新增的 7 只
V1_NEW_SYMBOLS = [
    "ORCL.US",
    "PLTR.US",
    "SMCI.US",
    "MU.US",
    "MRVL.US",
    "QCOM.US",
    "INTC.US",
]

DEFAULT_COUNT = 1000  # ~4 年交易日


def main():
    parser = argparse.ArgumentParser(description="下载 V1 新增标的的历史 K 线")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"每只下载多少根日 K（默认 {DEFAULT_COUNT}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新下载（即使已有数据）",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=V1_NEW_SYMBOLS,
        help="要下载的 symbol 列表（默认 V1 新增 7 只）",
    )
    args = parser.parse_args()

    db = DatabaseManager()
    client = LongPortClient()

    stats = {"downloaded": [], "skipped": [], "failed": []}

    for symbol in args.symbols:
        # 检查是否已有（直接用 SQL 看计数，避免加载全量数据）
        if not args.force:
            with db._get_conn() as conn:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM kline_data WHERE symbol = ? AND period = '1d'",
                    (symbol,),
                ).fetchone()[0]
            if cnt > 50:  # 至少有 50 根才算"已下载"
                logger.info(
                    f"[V1 K线] {symbol} 已有 {cnt} 根，skip（用 --force 覆盖）"
                )
                stats["skipped"].append(symbol)
                continue

        try:
            logger.info(f"[V1 K线] 下载 {symbol} ×{args.count} 根...")
            df = client.get_history_kline(
                symbol=symbol,
                period="1d",
                count=args.count,
                adjust="qfq",
            )
            if df.empty:
                logger.warning(f"[V1 K线] {symbol} 返回空，skip")
                stats["failed"].append((symbol, "empty response"))
                continue

            db.save_kline(symbol, df, period="1d", adjust_type="qfq")
            first_date = df["date"].min().strftime("%Y-%m-%d")
            last_date = df["date"].max().strftime("%Y-%m-%d")
            logger.info(
                f"[V1 K线] ✅ {symbol} 保存 {len(df)} 根，区间 {first_date} → {last_date}"
            )
            stats["downloaded"].append((symbol, len(df), first_date, last_date))
        except Exception as e:
            logger.exception(f"[V1 K线] {symbol} 失败")
            stats["failed"].append((symbol, str(e)[:100]))

    # 总结
    print("\n" + "=" * 60)
    print("V1 K 线下载总结")
    print("=" * 60)
    print(f"✅ 下载成功: {len(stats['downloaded'])} 只")
    for symbol, n, start, end in stats["downloaded"]:
        print(f"    {symbol}: {n} 根 ({start} → {end})")
    print(f"⏭ 跳过（已存在）: {len(stats['skipped'])} 只  {stats['skipped']}")
    if stats["failed"]:
        print(f"❌ 失败: {len(stats['failed'])} 只")
        for symbol, err in stats["failed"]:
            print(f"    {symbol}: {err}")
    print("=" * 60)

    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())

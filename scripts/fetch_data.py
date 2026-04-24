#!/usr/bin/env python3
"""
手动数据采集脚本。
用法：
    python scripts/fetch_data.py --symbols AAPL.US,MSFT.US --days 365
    python scripts/fetch_data.py --config  (使用 config/strategies.yaml 中的 watchlist)
"""

import argparse
import os
import sys

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager


def main():
    parser = argparse.ArgumentParser(description="Fetch historical market data")
    parser.add_argument(
        "--symbols",
        type=str,
        help="Comma-separated list of symbols (e.g., AAPL.US,MSFT.US)",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Use watchlist from config/strategies.yaml",
    )
    parser.add_argument(
        "--days", type=int, default=365, help="Number of days of history (default: 365)"
    )
    parser.add_argument(
        "--period", type=str, default="1d", help="Kline period (default: 1d)"
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="qfq",
        choices=["qfq", "hfq", "none"],
        help="Adjust type (default: qfq)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="yfinance",
        choices=["yfinance", "longport"],
        help="History data source (default: yfinance)",
    )
    parser.add_argument(
        "--export-csv", action="store_true", help="Also export to CSV files"
    )

    args = parser.parse_args()

    # 确定标的列表
    symbols = []
    if args.config:
        import yaml
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "strategies.yaml",
        )
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)
        symbols = cfg.get("watchlist", [])
        logger.info(f"Loaded {len(symbols)} symbols from config")
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        parser.error("Please specify --symbols or --config")

    if not symbols:
        logger.error("No symbols to fetch")
        return

    # 初始化
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data_cache",
        "quant.db",
    )
    db = DatabaseManager(db_path)
    fetcher = DataFetcher(db=db, history_source=args.source)

    # 日期范围：yfinance 优先用 start/end，LongPort 用 count
    from datetime import datetime, timedelta

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    count = min(args.days, 5000)  # yfinance 无硬上限；LongPort 单次有限制

    logger.info(
        f"Source: {args.source}, fetching {args.days} days ({start_date} ~ {end_date}) "
        f"of {args.period} data for {len(symbols)} symbols"
    )
    logger.info(f"Adjust type: {args.adjust}")

    # 执行采集
    results = fetcher.fetch_history(
        symbols=symbols,
        period=args.period,
        count=count,
        adjust=args.adjust,
        start_date=start_date,
        end_date=end_date,
    )

    # 输出摘要
    print("\n" + "=" * 60)
    print(f"{'Symbol':<12} {'Bars':>6} {'Start':>12} {'End':>12} {'Last Close':>12}")
    print("-" * 60)
    for symbol, df in results.items():
        if not df.empty:
            print(
                f"{symbol:<12} {len(df):>6} "
                f"{str(df['date'].iloc[0])[:10]:>12} "
                f"{str(df['date'].iloc[-1])[:10]:>12} "
                f"{df['close'].iloc[-1]:>12.2f}"
            )
    print("=" * 60)

    # 可选导出 CSV
    if args.export_csv:
        csv_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data_cache",
            "csv",
        )
        for symbol in results:
            fetcher.export_to_csv(symbol, csv_dir, args.period)
        logger.info(f"CSV files exported to {csv_dir}")


if __name__ == "__main__":
    main()

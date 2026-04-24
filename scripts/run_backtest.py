#!/usr/bin/env python3
"""
回测运行脚本。
用法：
    python scripts/run_backtest.py --strategy ma_cross --symbol AAPL.US
    python scripts/run_backtest.py --strategy rsi --symbol MSFT.US --capital 50000
    python scripts/run_backtest.py --strategy ma_cross --config  (使用 watchlist 全部标的)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from loguru import logger

from src.backtest.engine import BacktestEngine
from src.backtest.visualizer import BacktestVisualizer
from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager
from src.strategy.strategy_manager import STRATEGY_REGISTRY
from src.utils.config_loader import ConfigLoader


def main():
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        choices=list(STRATEGY_REGISTRY.keys()),
        help="Strategy name",
    )
    parser.add_argument("--symbol", type=str, help="Symbol (e.g., AAPL.US)")
    parser.add_argument(
        "--config", action="store_true", help="Run on all watchlist symbols"
    )
    parser.add_argument("--capital", type=float, default=None, help="Initial capital ($)")
    parser.add_argument("--days", type=int, default=365, help="History days")
    parser.add_argument(
        "--output", type=str, default="output", help="Output directory"
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip chart generation")
    parser.add_argument(
        "--sl-mode",
        type=str,
        default=None,
        choices=["legacy", "atr_442"],
        help="Stop-loss mode override (default: read from risk.yaml)",
    )
    parser.add_argument(
        "--pos-mode",
        type=str,
        default=None,
        choices=["fixed_pct", "risk_based_atr", "legacy_cash95", "kelly", "equal_weight"],
        help="Position sizing mode override",
    )

    args = parser.parse_args()

    # 加载配置
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_config = config_loader.get_strategies_config()
    risk_config = config_loader.get_risk_config()

    # CLI 覆盖
    if args.sl_mode:
        risk_config.setdefault("stop_loss", {})["mode"] = args.sl_mode
    if args.pos_mode:
        risk_config.setdefault("position", {})["mode"] = args.pos_mode

    # 确定标的列表
    symbols = []
    if args.config:
        symbols = strategies_config.get("watchlist", [])
    elif args.symbol:
        symbols = [args.symbol]
    else:
        parser.error("Please specify --symbol or --config")

    # 初始化模块
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)
    fetcher = DataFetcher(db=db)
    engine = BacktestEngine(risk_config)
    visualizer = BacktestVisualizer(os.path.join(project_root, args.output))

    # 初始化策略
    strategy_class = STRATEGY_REGISTRY[args.strategy]
    strategy_config = strategies_config.get(args.strategy, {})

    all_results = {}

    for symbol in symbols:
        logger.info(f"\n{'='*60}\n  Backtesting {args.strategy} on {symbol}\n{'='*60}")

        # 加载数据
        data = fetcher.load_data(symbol, period="1d")
        if data.empty:
            logger.warning(f"No data for {symbol}, skipping")
            continue

        # 创建策略实例
        strategy = strategy_class()
        strategy.init(strategy_config)

        # 运行回测
        result = engine.run(
            strategy=strategy,
            data=data,
            symbol=symbol,
            initial_capital=args.capital,
        )

        all_results[symbol] = result

        # 生成图表
        if not args.no_plot and "equity_curve" in result:
            visualizer.plot_equity_curve(
                result["equity_curve"],
                title=f"{args.strategy} on {symbol}",
                save_as=os.path.join(
                    project_root, args.output, f"{symbol}_{args.strategy}_equity.png"
                ),
            )
            if result.get("trades"):
                visualizer.plot_trades_on_kline(
                    data,
                    result["trades"],
                    symbol=symbol,
                    save_as=os.path.join(
                        project_root, args.output, f"{symbol}_{args.strategy}_kline.png"
                    ),
                )

        # 保存回测结果到数据库
        if "metrics" in result:
            db.save_backtest_result(
                {
                    **result["metrics"],
                    "params_json": json.dumps(strategy.get_params()),
                }
            )

    # 多标的对比
    if len(all_results) > 1 and not args.no_plot:
        metrics_map = {
            sym: res["metrics"] for sym, res in all_results.items() if "metrics" in res
        }
        visualizer.plot_metrics_comparison(metrics_map)

    # 打印汇总
    print(f"\n{'='*80}")
    print(f"  BACKTEST SUMMARY: {args.strategy}")
    print(f"{'='*80}")
    print(
        f"{'Symbol':<12} {'Return':>10} {'Annual':>10} {'MaxDD':>10} "
        f"{'Sharpe':>8} {'Calmar':>8} {'WinRate':>8} {'Trades':>7}"
    )
    print("-" * 80)
    for symbol, result in all_results.items():
        m = result.get("metrics", {})
        calmar = m.get("calmar_ratio")
        calmar_s = f"{calmar:>8.2f}" if calmar is not None else f"{'N/A':>8}"
        print(
            f"{symbol:<12} "
            f"{m.get('total_return', 0):>10.2%} "
            f"{m.get('annual_return', 0):>10.2%} "
            f"{m.get('max_drawdown', 0):>10.2%} "
            f"{m.get('sharpe_ratio', 0):>8.2f} "
            f"{calmar_s} "
            f"{m.get('win_rate', 0):>8.2%} "
            f"{m.get('trade_count', 0):>7d}"
        )
    print("=" * 80)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
实盘交易启动脚本。

⚠️ 警告: 此脚本将使用真实资金进行交易！
请确保已:
1. 完成模拟交易验证
2. 配置好长桥 API 密钥
3. 确认风控参数合理

用法：
    python scripts/run_live_trade.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager
from src.data.longport_client import LongPortClient
from src.strategy.strategy_manager import StrategyManager
from src.trader.live_trader import LiveTrader
from src.utils.config_loader import ConfigLoader


def main():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_config = config_loader.get_strategies_config()
    risk_config = config_loader.get_risk_config()

    # ============ SAFETY CONFIRMATION ============
    print("\n" + "=" * 60)
    print("  ⚠️  WARNING: LIVE TRADING MODE")
    print("  This will use REAL MONEY from your LongPort account!")
    print("=" * 60)
    print(f"\n  Market:     US Stocks")
    print(f"  Strategies: {strategies_config.get('active_strategies', [])}")
    print(f"  Watchlist:  {strategies_config.get('watchlist', [])[:5]}...")
    print(f"\n  Risk Limits:")
    print(f"    Max single position: {risk_config.get('position', {}).get('max_single_position_pct', 0.2):.0%}")
    print(f"    Stop loss:           {risk_config.get('stop_loss', {}).get('per_trade_stop_loss_pct', 0.05):.0%}")
    print(f"    Max daily loss:      {risk_config.get('daily_limits', {}).get('max_daily_loss_pct', 0.03):.0%}")
    print(f"    Max drawdown:        {risk_config.get('portfolio_limits', {}).get('max_drawdown_pct', 0.10):.0%}")

    confirmation = input("\n  Type 'CONFIRM' to proceed with live trading: ")
    if confirmation != "CONFIRM":
        print("  Cancelled. Use paper trading mode instead.")
        return

    # ============ INITIALIZE ============
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)
    client = LongPortClient()
    fetcher = DataFetcher(client=client, db=db)
    strategy_mgr = StrategyManager(strategies_config)

    trader = LiveTrader(client=client, risk_config=risk_config, db=db)
    trader.confirm_live_trading()

    watchlist = strategies_config.get("watchlist", [])

    logger.warning("🔴 LIVE TRADING ACTIVE")

    # 获取数据
    data_map = {}
    for symbol in watchlist:
        data = fetcher.load_data(symbol, period="1d")
        if not data.empty:
            data_map[symbol] = data

    # 运行策略
    signals_map = strategy_mgr.run_watchlist(watchlist, data_map)

    # 执行信号
    for symbol, signals in signals_map.items():
        for signal in signals:
            logger.info(f"Executing live signal: {signal}")
            trader.execute_signal(signal)

    # 打印当前持仓
    positions = trader.sync_positions()
    if positions:
        print("\n  Current Positions:")
        for sym, pos in positions.items():
            print(f"    {sym}: qty={pos['quantity']}, cost={pos['cost_price']:.2f}")

    logger.info("Live trading cycle complete")


if __name__ == "__main__":
    main()

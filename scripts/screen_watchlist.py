#!/usr/bin/env python3
"""
Watchlist 筛选工具（阶段8 扩容用）

对 strategies.yaml 里的 watchlist 跑三个 legacy 策略，输出每只标的在每个策略下的：
- 总收益率
- 年化收益率
- 最大回撤
- 夏普比
- 胜率
- 交易次数

用途：
- 扩大 watchlist 后，识别"哪些标的在哪些策略下表现好"
- 为 per-symbol strategy 映射提供依据
- 筛掉所有策略下都亏钱/低胜率的标的

用法：
    python scripts/screen_watchlist.py
    python scripts/screen_watchlist.py --capital 100000 --period 1000
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine
from src.data.database import DatabaseManager
from src.strategy.strategy_manager import STRATEGY_REGISTRY
from src.utils.config_loader import ConfigLoader


def run_single(
    engine: BacktestEngine,
    strategy_cls,
    strategy_params: dict,
    symbol: str,
    data: pd.DataFrame,
    capital: float,
) -> dict:
    """跑单次回测，返回核心指标"""
    try:
        # 实例化策略
        strategy = strategy_cls()
        strategy.init(strategy_params)
        result = engine.run(
            strategy=strategy,
            data=data,
            symbol=symbol,
            initial_capital=capital,
        )
        # 真实结果在 result["metrics"] 里
        m = result.get("metrics", {}) if isinstance(result, dict) else {}
        return {
            "symbol": symbol,
            "total_return": m.get("total_return", 0) * 100,
            "annual_return": m.get("annual_return", 0) * 100,
            "max_drawdown": m.get("max_drawdown", 0) * 100,
            "sharpe": m.get("sharpe_ratio", 0) or 0,
            "win_rate": m.get("win_rate", 0) * 100,
            "trades": m.get("trade_count", 0),
            "calmar": m.get("calmar_ratio") or 0,
        }
    except Exception as e:
        logger.warning(f"  ❌ {symbol} failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=100000, help="初始资金")
    parser.add_argument("--period", type=int, default=1000, help="回测天数（从最新往回推）")
    parser.add_argument("--output", type=str, default="output/watchlist_screen.csv")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_cfg = config_loader.get_strategies_config()
    risk_cfg = config_loader.get_risk_config()
    watchlist = strategies_cfg.get("watchlist", [])

    logger.info(f"Watchlist 筛选：{len(watchlist)} 只标的 × 3 策略 = {len(watchlist)*3} 次回测")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    engine = BacktestEngine(risk_config=risk_cfg)

    strategies = ["ma_cross", "rsi", "momentum"]
    rows = []

    for sym in watchlist:
        df = db.load_kline(sym, period="1d")
        if df is None or df.empty or len(df) < 100:
            logger.warning(f"⚠️ {sym} 数据不足（{len(df) if df is not None else 0} bars），跳过")
            continue

        # 只取最近 period 天
        df = df.tail(args.period).reset_index(drop=True)

        for strat_name in strategies:
            strategy_cls = STRATEGY_REGISTRY.get(strat_name)
            if strategy_cls is None:
                continue
            params = strategies_cfg.get(strat_name, {}) or {}

            logger.info(f"▶️ {sym} × {strat_name}")
            r = run_single(engine, strategy_cls, params, sym, df, args.capital)
            if r:
                r["strategy"] = strat_name
                rows.append(r)

    if not rows:
        logger.error("❌ 没有任何成功的回测")
        return

    df_result = pd.DataFrame(rows)
    # 排序：先按策略，再按 Calmar 降序
    df_result = df_result.sort_values(["strategy", "calmar"], ascending=[True, False])

    # 重排列
    df_result = df_result[[
        "strategy", "symbol", "total_return", "annual_return",
        "max_drawdown", "sharpe", "calmar", "win_rate", "trades"
    ]]

    # 保存
    output_path = os.path.join(project_root, args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df_result.to_csv(output_path, index=False)

    # 打印汇总
    print("\n" + "=" * 100)
    print(f"📊 Watchlist 筛选结果（{len(df_result)} 次回测）")
    print("=" * 100)

    for strat in strategies:
        sub = df_result[df_result["strategy"] == strat]
        if sub.empty:
            continue
        print(f"\n🎯 [{strat}] 策略下的表现（按 Calmar 降序）:")
        print(sub[["symbol", "total_return", "annual_return", "max_drawdown",
                   "calmar", "win_rate", "trades"]].to_string(index=False,
                   float_format=lambda x: f"{x:.2f}"))

    # 整体洞察
    print("\n" + "=" * 100)
    print("📌 筛选建议")
    print("=" * 100)

    # 找出"每个策略下最赚钱的 top 5"
    for strat in strategies:
        sub = df_result[df_result["strategy"] == strat]
        if sub.empty:
            continue
        top = sub.nlargest(5, "calmar")
        print(f"\n✅ [{strat}] Top 5（高 Calmar）: {', '.join(top['symbol'].tolist())}")

    # 找出"所有策略都亏钱"的标的（候选删除）
    bad = df_result.groupby("symbol")["total_return"].max().reset_index()
    bad = bad[bad["total_return"] < 0].sort_values("total_return")
    if not bad.empty:
        print(f"\n⚠️ 在所有策略下都亏钱（Top 5 最差）: "
              f"{', '.join(bad.head(5)['symbol'].tolist())}")

    print(f"\n📄 完整结果：{output_path}")


if __name__ == "__main__":
    main()

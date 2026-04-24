#!/usr/bin/env python3
"""
Phase 0 Baseline - 阶段 0 基线回测
用当前（旧）风控配置跑 MA/RSI/Momentum × watchlist 9 只股票，
固化为阶段 1 改造前的基线数据，后续 A/B 对比使用。

输出:
    output/phase0_baseline.csv  - 明细数据
    output/phase0_baseline.md   - Markdown 对比表
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine
from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager
from src.strategy.strategy_manager import STRATEGY_REGISTRY
from src.utils.config_loader import ConfigLoader


def main():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_config = config_loader.get_strategies_config()
    risk_config = config_loader.get_risk_config()

    symbols = strategies_config.get("watchlist", [])
    strategies = ["ma_cross", "rsi", "momentum"]

    # 关闭回测中过多的 info 日志
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    fetcher = DataFetcher(db=db)
    engine = BacktestEngine(risk_config)

    rows = []
    total = len(symbols) * len(strategies)
    done = 0
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)
    progress_log = os.path.join(output_dir, "phase0_progress.log")
    with open(progress_log, "w") as pf:
        pf.write(f"[{datetime.now()}] START total={total}\n")
        pf.flush()

    for strat_name in strategies:
        strat_config = strategies_config.get(strat_name, {})
        for symbol in symbols:
            done += 1
            msg = f"[{done}/{total}] {strat_name} × {symbol}"
            with open(progress_log, "a") as pf:
                pf.write(f"[{datetime.now()}] {msg} ...\n")
                pf.flush()
            try:
                data = fetcher.load_data(symbol, period="1d")
                if data.empty:
                    rows.append({"symbol": symbol, "strategy": strat_name, "status": "NO_DATA"})
                    continue

                cls = STRATEGY_REGISTRY[strat_name]
                strategy = cls()
                strategy.init(strat_config)

                result = engine.run(strategy=strategy, data=data, symbol=symbol)
                m = result.get("metrics", {})
                rows.append({
                    "symbol": symbol,
                    "strategy": strat_name,
                    "status": "OK",
                    "total_return": m.get("total_return", 0),
                    "annual_return": m.get("annual_return", 0),
                    "max_drawdown": m.get("max_drawdown", 0),
                    "sharpe_ratio": m.get("sharpe_ratio", 0),
                    "sortino_ratio": m.get("sortino_ratio", 0),
                    "win_rate": m.get("win_rate", 0),
                    "profit_loss_ratio": m.get("profit_loss_ratio", 0),
                    "trade_count": m.get("trade_count", 0),
                    "avg_holding_days": m.get("avg_holding_days", 0),
                })
                with open(progress_log, "a") as pf:
                    pf.write(f"[{datetime.now()}]   OK return={m.get('total_return',0):.2%} trades={m.get('trade_count',0)}\n")
                    pf.flush()
            except Exception as e:
                rows.append({"symbol": symbol, "strategy": strat_name, "status": f"ERROR: {e}"})
                with open(progress_log, "a") as pf:
                    pf.write(f"[{datetime.now()}]   ERROR {e}\n")
                    pf.flush()

    df = pd.DataFrame(rows)
    csv_path = os.path.join(output_dir, "phase0_baseline.csv")
    df.to_csv(csv_path, index=False)
    print(f"\n✓ CSV saved: {csv_path}")

    # Markdown 报告
    md_path = os.path.join(output_dir, "phase0_baseline.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 阶段 0 基线 — 旧风控回测\n\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(
            f"- 数据: watchlist {len(symbols)} 只 × 1000 根日线（2022-04 ~ 2026-04, qfq 前复权）\n"
            f"- 风控: stop_loss=-5% / take_profit=+15% / trailing_stop=5% / 单笔 10% 仓位\n"
            f"- 初始资金: $10,000\n\n"
        )

        for strat in strategies:
            f.write(f"## {strat}\n\n")
            sub = df[df["strategy"] == strat]
            if sub.empty:
                f.write("_(无数据)_\n\n")
                continue
            f.write("| Symbol | 总收益 | 年化 | 最大回撤 | Sharpe | Sortino | 胜率 | 盈亏比 | 交易次数 | 平均持仓 |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|\n")
            for _, r in sub.iterrows():
                if r.get("status") != "OK":
                    f.write(f"| {r['symbol']} | — | — | — | — | — | — | — | — | {r.get('status','?')} |\n")
                    continue
                f.write(
                    f"| {r['symbol']} | {r['total_return']:.2%} | {r['annual_return']:.2%} | "
                    f"{r['max_drawdown']:.2%} | {r['sharpe_ratio']:.2f} | {r['sortino_ratio']:.2f} | "
                    f"{r['win_rate']:.2%} | {r['profit_loss_ratio']:.2f} | "
                    f"{int(r['trade_count'])} | {r['avg_holding_days']:.1f} |\n"
                )
            f.write("\n")

    print(f"✓ Markdown saved: {md_path}")
    print(f"\n阶段 0 基线已固化，{len(rows)} 个回测完成")


if __name__ == "__main__":
    main()

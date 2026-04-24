#!/usr/bin/env python3
"""
A/B Comparison - 新旧风控模式对比回测

阶段 3 第一轮：3 标的（AAPL/META/TSLA） × 3 策略 × 2 模式 = 18 次回测
--full 开关扩到 9 标的 = 54 次

对照组：
- A (legacy):  stop_loss.mode=legacy  + position.mode=legacy_cash95（严格复现基线）
- B (new):     stop_loss.mode=atr_442 + position.mode=risk_based_atr（按策略分级参数）

输出：
- output/ab_comparison_YYYYMMDD_HHMMSS.csv
- output/ab_comparison_YYYYMMDD_HHMMSS.md
"""

import argparse
import copy
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine
from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager
from src.strategy.strategy_manager import STRATEGY_REGISTRY
from src.utils.config_loader import ConfigLoader


ROUND1_SYMBOLS = ["AAPL.US", "META.US", "TSLA.US"]
STRATEGIES = ["ma_cross", "rsi", "momentum"]
MODES = [
    ("legacy", "legacy", "legacy_cash95"),  # 基线对照（95%现金重仓）
    ("new", "atr_442", "risk_based_atr"),   # 新模式（ATR+442）
]


def run_single(
    engine: BacktestEngine,
    strategy_class,
    strategy_config: dict,
    data: pd.DataFrame,
    symbol: str,
) -> dict:
    strategy = strategy_class()
    strategy.init(strategy_config)
    result = engine.run(strategy=strategy, data=data, symbol=symbol)
    m = result["metrics"]
    exit_tags = {}
    for t in result.get("trades", []):
        k = t.get("exit_tag", "") or "sig"
        exit_tags[k] = exit_tags.get(k, 0) + 1
    return {
        "total_return": m.get("total_return", 0),
        "annual_return": m.get("annual_return", 0),
        "max_drawdown": m.get("max_drawdown", 0),
        "sharpe_ratio": m.get("sharpe_ratio", 0),
        "sortino_ratio": m.get("sortino_ratio", 0),
        "calmar_ratio": m.get("calmar_ratio"),
        "win_rate": m.get("win_rate", 0),
        "profit_loss_ratio": m.get("profit_loss_ratio", 0),
        "trade_count": m.get("trade_count", 0),
        "avg_holding_days": m.get("avg_holding_days", 0),
        "exit_tags": exit_tags,
    }


def build_engine(base_risk_cfg: dict, sl_mode: str, pos_mode: str) -> BacktestEngine:
    cfg = copy.deepcopy(base_risk_cfg)
    cfg.setdefault("stop_loss", {})["mode"] = sl_mode
    cfg.setdefault("position", {})["mode"] = pos_mode
    return BacktestEngine(cfg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="扩到全量 9 标的 × 54 次")
    parser.add_argument("--output", default="output", help="输出目录")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loader = ConfigLoader(os.path.join(project_root, "config"))
    risk_cfg = loader.get_risk_config()
    strategies_cfg = loader.get_strategies_config()

    watchlist = strategies_cfg.get("watchlist", [])
    symbols = watchlist if args.full else ROUND1_SYMBOLS
    # 确保 ROUND1_SYMBOLS 在 watchlist 中
    symbols = [s for s in symbols if s in watchlist] or ROUND1_SYMBOLS

    # 静默 backtrader/loguru 干扰
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    fetcher = DataFetcher(db=db, history_source="longport")

    total = len(symbols) * len(STRATEGIES) * len(MODES)
    print(f"[A/B] Running {total} backtests ({len(symbols)} symbols × "
          f"{len(STRATEGIES)} strategies × {len(MODES)} modes)")

    rows = []
    t_start = time.time()
    done = 0
    for mode_name, sl_mode, pos_mode in MODES:
        engine = build_engine(risk_cfg, sl_mode, pos_mode)
        for sn in STRATEGIES:
            strategy_cls = STRATEGY_REGISTRY[sn]
            strat_cfg = strategies_cfg.get(sn, {})
            for sym in symbols:
                done += 1
                data = fetcher.load_data(sym, period="1d")
                if data.empty:
                    print(f"[{done}/{total}] {mode_name} {sn} {sym}: NO_DATA")
                    continue
                t0 = time.time()
                try:
                    r = run_single(engine, strategy_cls, strat_cfg, data, sym)
                    r["mode"] = mode_name
                    r["sl_mode"] = sl_mode
                    r["pos_mode"] = pos_mode
                    r["strategy"] = sn
                    r["symbol"] = sym
                    r["status"] = "OK"
                    rows.append(r)
                    t1 = time.time()
                    print(f"[{done}/{total}] {mode_name:<6} {sn:<10} {sym:<10} "
                          f"ret={r['total_return']:>7.2%} trades={r['trade_count']:>3d} "
                          f"dd={r['max_drawdown']:>6.2%} ({t1-t0:.1f}s)")
                except Exception as e:
                    rows.append({
                        "mode": mode_name, "sl_mode": sl_mode, "pos_mode": pos_mode,
                        "strategy": sn, "symbol": sym, "status": f"ERR: {e}",
                    })
                    print(f"[{done}/{total}] {mode_name} {sn} {sym}: ERR {e}")

    total_time = time.time() - t_start
    print(f"\nAll done in {total_time:.1f}s ({total_time/total:.1f}s/run)")

    # === 输出 ===
    output_dir = os.path.join(project_root, args.output)
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"ab_comparison_{ts}.csv")
    md_path = os.path.join(output_dir, f"ab_comparison_{ts}.md")

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"✓ CSV  -> {csv_path}")

    # Markdown 汇总
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# A/B 对比回测 — {ts}\n\n")
        f.write(f"- 标的: {', '.join(symbols)}\n")
        f.write(f"- 策略: {', '.join(STRATEGIES)}\n")
        f.write(f"- 模式: legacy (95%现金重仓+-5%/+15%止损) VS new (ATR动态+442分批)\n")
        f.write(f"- 总耗时: {total_time:.1f}s\n\n")

        for sn in STRATEGIES:
            f.write(f"## 策略: {sn}\n\n")
            f.write("| Symbol | Mode | 总收益 | 年化 | MaxDD | Sharpe | Calmar | 胜率 | 盈亏比 | 交易次数 | Exit Tags |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
            for sym in symbols:
                for mode_name, _, _ in MODES:
                    sub = [r for r in rows
                           if r.get("strategy") == sn and r.get("symbol") == sym
                           and r.get("mode") == mode_name and r.get("status") == "OK"]
                    if not sub:
                        continue
                    r = sub[0]
                    cr = r.get("calmar_ratio")
                    cr_s = f"{cr:.2f}" if cr is not None else "N/A"
                    tags = r.get("exit_tags", {})
                    tags_s = ", ".join(f"{k}:{v}" for k, v in tags.items())
                    f.write(f"| {sym} | {mode_name} | "
                            f"{r['total_return']:.2%} | {r['annual_return']:.2%} | "
                            f"{r['max_drawdown']:.2%} | {r['sharpe_ratio']:.2f} | "
                            f"{cr_s} | {r['win_rate']:.2%} | "
                            f"{r['profit_loss_ratio']:.2f} | {r['trade_count']} | "
                            f"{tags_s} |\n")
            f.write("\n")

        # 对比汇总：每个 symbol×strategy 下 new vs legacy 的改善
        f.write("## 改善汇总 (new vs legacy)\n\n")
        f.write("| Symbol | Strategy | ΔTotalReturn | ΔMaxDD | ΔSharpe | ΔCalmar |\n")
        f.write("|---|---|---|---|---|---|\n")
        for sym in symbols:
            for sn in STRATEGIES:
                legacy = next((r for r in rows
                               if r.get("mode") == "legacy" and r.get("strategy") == sn
                               and r.get("symbol") == sym and r.get("status") == "OK"), None)
                new = next((r for r in rows
                            if r.get("mode") == "new" and r.get("strategy") == sn
                            and r.get("symbol") == sym and r.get("status") == "OK"), None)
                if not legacy or not new:
                    continue
                d_ret = new["total_return"] - legacy["total_return"]
                d_dd = new["max_drawdown"] - legacy["max_drawdown"]
                d_sharpe = new["sharpe_ratio"] - legacy["sharpe_ratio"]
                cr_l = legacy.get("calmar_ratio")
                cr_n = new.get("calmar_ratio")
                d_calmar = (cr_n - cr_l) if (cr_l is not None and cr_n is not None) else None
                d_calmar_s = f"{d_calmar:+.2f}" if d_calmar is not None else "N/A"
                f.write(f"| {sym} | {sn} | {d_ret:+.2%} | {d_dd:+.2%} | "
                        f"{d_sharpe:+.2f} | {d_calmar_s} |\n")

    print(f"✓ MD   -> {md_path}")


if __name__ == "__main__":
    main()

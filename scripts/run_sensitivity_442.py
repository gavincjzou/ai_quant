#!/usr/bin/env python3
"""
442 Parameter Sensitivity Scan - 4-4-2 止盈参数敏感性扫描

在 MA 策略 × [AAPL/META/TSLA] 上扫描：
- atr_stop_mult ∈ [1.5, 2.0, 2.5]
- tp1_rr       ∈ [0.8, 1.0, 1.5]
共 3×3 = 9 组参数，每组跑 3 个标的 = 27 次回测

输出：
- output/sensitivity_442_YYYYMMDD_HHMMSS.csv
- output/sensitivity_442_YYYYMMDD_HHMMSS.md（热力图式 pivot 表）
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


SCAN_SYMBOLS = ["AAPL.US", "META.US", "TSLA.US"]
SCAN_STRATEGY = "ma_cross"
STOP_MULTS = [1.5, 2.0, 2.5]
TP1_RRS = [0.8, 1.0, 1.5]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loader = ConfigLoader(os.path.join(project_root, "config"))
    base_risk = loader.get_risk_config()
    strats_cfg = loader.get_strategies_config()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    fetcher = DataFetcher(db=db, history_source="longport")

    total = len(SCAN_SYMBOLS) * len(STOP_MULTS) * len(TP1_RRS)
    print(f"[SENS] 442 param scan on {SCAN_STRATEGY}, {total} runs")

    rows = []
    t_start = time.time()
    done = 0
    for stop_mult in STOP_MULTS:
        for tp1_rr in TP1_RRS:
            # TP2=TP1*2, TP3=TP1*3 保持阶梯
            tp2_rr = tp1_rr * 2
            tp3_rr = tp1_rr * 3

            cfg = copy.deepcopy(base_risk)
            cfg.setdefault("stop_loss", {})["mode"] = "atr_442"
            cfg.setdefault("position", {})["mode"] = "risk_based_atr"
            # 覆盖 atr_442 全局参数
            cfg["stop_loss"].setdefault("atr_442", {})
            cfg["stop_loss"]["atr_442"]["atr_stop_mult"] = stop_mult
            cfg["stop_loss"]["atr_442"]["tp1_rr"] = tp1_rr
            cfg["stop_loss"]["atr_442"]["tp2_rr"] = tp2_rr
            cfg["stop_loss"]["atr_442"]["tp3_rr"] = tp3_rr
            # 同时覆盖 per_strategy_overrides (ma_cross)，保证"每策略覆盖"也用同样值
            cfg.setdefault("per_strategy_overrides", {}).setdefault("ma_cross", {})
            cfg["per_strategy_overrides"]["ma_cross"]["atr_stop_mult"] = stop_mult
            cfg["per_strategy_overrides"]["ma_cross"]["tp1_rr"] = tp1_rr
            cfg["per_strategy_overrides"]["ma_cross"]["tp2_rr"] = tp2_rr
            cfg["per_strategy_overrides"]["ma_cross"]["tp3_rr"] = tp3_rr

            engine = BacktestEngine(cfg)
            cls = STRATEGY_REGISTRY[SCAN_STRATEGY]
            strat_cfg = strats_cfg.get(SCAN_STRATEGY, {})

            for sym in SCAN_SYMBOLS:
                done += 1
                data = fetcher.load_data(sym, period="1d")
                if data.empty:
                    continue
                strat = cls(); strat.init(strat_cfg)
                t0 = time.time()
                res = engine.run(strat, data, sym)
                t1 = time.time()
                m = res["metrics"]
                tags = {}
                for t in res.get("trades", []):
                    k = t.get("exit_tag", "") or "sig"
                    tags[k] = tags.get(k, 0) + 1
                rows.append({
                    "stop_mult": stop_mult, "tp1_rr": tp1_rr,
                    "symbol": sym,
                    "total_return": m.get("total_return", 0),
                    "annual_return": m.get("annual_return", 0),
                    "max_drawdown": m.get("max_drawdown", 0),
                    "sharpe_ratio": m.get("sharpe_ratio", 0),
                    "calmar_ratio": m.get("calmar_ratio"),
                    "trade_count": m.get("trade_count", 0),
                    "win_rate": m.get("win_rate", 0),
                    "tp1_hits": tags.get("risk_442_tp1", 0),
                    "tp2_hits": tags.get("risk_442_tp2", 0),
                    "tp3_hits": tags.get("risk_442_tp3", 0),
                    "stop_hits": tags.get("risk_442_stop", 0),
                })
                print(f"[{done}/{total}] stop={stop_mult} tp1={tp1_rr} {sym:<10} "
                      f"ret={m['total_return']:>7.2%} dd={m['max_drawdown']:>6.2%} "
                      f"TP1={tags.get('risk_442_tp1',0)} TP2={tags.get('risk_442_tp2',0)} "
                      f"TP3={tags.get('risk_442_tp3',0)} stop={tags.get('risk_442_stop',0)} "
                      f"({t1-t0:.1f}s)")

    print(f"\nDone in {time.time()-t_start:.1f}s")

    # === 输出 ===
    output_dir = os.path.join(project_root, args.output)
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"sensitivity_442_{ts}.csv")
    md_path = os.path.join(output_dir, f"sensitivity_442_{ts}.md")

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"✓ CSV -> {csv_path}")

    # 热力图式 pivot
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 442 参数敏感性扫描 — {ts}\n\n")
        f.write(f"- 策略: {SCAN_STRATEGY}\n")
        f.write(f"- 标的: {', '.join(SCAN_SYMBOLS)}\n")
        f.write(f"- 扫描: atr_stop_mult × tp1_rr = {len(STOP_MULTS)}×{len(TP1_RRS)} = 9 组\n\n")

        for sym in SCAN_SYMBOLS:
            sub = df[df["symbol"] == sym]
            f.write(f"## {sym}\n\n")
            for metric_name, col in [
                ("总收益", "total_return"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe_ratio"),
                ("Calmar", "calmar_ratio"),
            ]:
                f.write(f"### {metric_name}\n\n")
                pivot = sub.pivot(index="stop_mult", columns="tp1_rr", values=col)
                # 美化表头
                header = "| stop_mult \\\\ tp1_rr |"
                sep = "|---|"
                for c in pivot.columns:
                    header += f" {c} |"
                    sep += "---|"
                f.write(header + "\n")
                f.write(sep + "\n")
                for idx in pivot.index:
                    row = f"| **{idx}** |"
                    for c in pivot.columns:
                        v = pivot.loc[idx, c]
                        if pd.isna(v):
                            row += " N/A |"
                        elif col in ("total_return", "max_drawdown", "win_rate"):
                            row += f" {v:.2%} |"
                        else:
                            row += f" {v:.2f} |"
                    f.write(row + "\n")
                f.write("\n")

            # TP hits 分布
            f.write("### Exit Tags 分布\n\n")
            f.write("| stop_mult | tp1_rr | TP1 | TP2 | TP3 | Stop |\n")
            f.write("|---|---|---|---|---|---|\n")
            for _, r in sub.iterrows():
                f.write(f"| {r['stop_mult']} | {r['tp1_rr']} | "
                        f"{r['tp1_hits']} | {r['tp2_hits']} | "
                        f"{r['tp3_hits']} | {r['stop_hits']} |\n")
            f.write("\n")

    print(f"✓ MD  -> {md_path}")


if __name__ == "__main__":
    main()

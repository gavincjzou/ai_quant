#!/usr/bin/env python3
"""
Risk Pct Sweep - 单笔风险预算扫描（阶段 5-A）

目标：在固定 atr_442 + risk_based_atr 模式下，扫描 7 档 single_trade_risk_pct，
      找 MaxDD ≤ 15% 硬约束下 Calmar 最高的参数组合。

扫描设计：
- 7 档 base risk_pct：2.0% / 2.5% / 3.0% / 3.5% / 4.0% / 5.0% / 6.0%
- 按策略分级同比例缩放（保持原有相对关系）：
    MA  = base × 0.75 （相对基线 1.5%/2.0% 的比例）
    RSI = base × 1.00
    Mom = base × 1.25
- 9 标的 × 3 策略 × 7 档 = 189 次回测
- 预估 ~3.5s/次 × 189 ≈ 11 分钟

输出：
- output/risk_pct_sweep_YYYYMMDD_HHMMSS.csv
- output/risk_pct_sweep_YYYYMMDD_HHMMSS.md （含 MaxDD 过滤 + Calmar 排序 + 推荐参数）
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


# 7 档 base risk_pct（单位：百分比小数）
DEFAULT_RISK_PCTS = [0.020, 0.025, 0.030, 0.035, 0.040, 0.050, 0.060]

# 按策略同比例缩放因子（基线 MA=1.5% / RSI=2% / Mom=2.5% → base=2% 对应 0.75/1.0/1.25）
STRATEGY_SCALE = {
    "ma_cross": 0.75,
    "rsi": 1.00,
    "momentum": 1.25,
}

STRATEGIES = ["ma_cross", "rsi", "momentum"]
MAX_DD_LIMIT = 0.15  # 硬约束：MaxDD ≤ 15%


def build_risk_cfg(base_cfg: dict, base_risk_pct: float) -> dict:
    """复制 base_cfg，强制使用 atr_442 + risk_based_atr，并按 base_risk_pct 缩放各策略。

    同时联动放宽 max_single_position_pct，否则 20% 上限会锁死高档位的仓位。
    策略：max_single = max(20%, base_rp × 10)，覆盖 2%→20%、3%→30%、6%→60%。
    """
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("stop_loss", {})["mode"] = "atr_442"
    cfg.setdefault("position", {})["mode"] = "risk_based_atr"
    cfg["position"]["single_trade_risk_pct"] = base_risk_pct

    # 联动放宽单票上限
    new_max_single = max(0.20, round(base_risk_pct * 10, 3))
    cfg["position"]["max_single_position_pct"] = new_max_single

    overrides = cfg.setdefault("per_strategy_overrides", {})
    for sn, scale in STRATEGY_SCALE.items():
        per = overrides.setdefault(sn, {})
        per["single_trade_risk_pct"] = round(base_risk_pct * scale, 4)
    return cfg


def run_single(engine: BacktestEngine, strategy_cls, strat_cfg: dict,
               data: pd.DataFrame, symbol: str) -> dict:
    strategy = strategy_cls()
    strategy.init(strat_cfg)
    result = engine.run(strategy=strategy, data=data, symbol=symbol)
    m = result["metrics"]
    return {
        "total_return": m.get("total_return", 0),
        "annual_return": m.get("annual_return", 0),
        "max_drawdown": m.get("max_drawdown", 0),
        "sharpe_ratio": m.get("sharpe_ratio", 0),
        "sortino_ratio": m.get("sortino_ratio", 0),
        "calmar_ratio": m.get("calmar_ratio"),
        "win_rate": m.get("win_rate", 0),
        "trade_count": m.get("trade_count", 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：仅 3 档 + 3 标的 (27次) 做 smoke test")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loader = ConfigLoader(os.path.join(project_root, "config"))
    base_risk_cfg = loader.get_risk_config()
    strategies_cfg = loader.get_strategies_config()

    watchlist = strategies_cfg.get("watchlist", [])
    if args.quick:
        symbols = ["AAPL.US", "META.US", "TSLA.US"]
        risk_pcts = [0.02, 0.04, 0.06]
    else:
        symbols = watchlist
        risk_pcts = DEFAULT_RISK_PCTS

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    fetcher = DataFetcher(db=db, history_source="longport")

    # 缓存各标的数据，避免反复 IO
    data_cache = {}
    for sym in symbols:
        data_cache[sym] = fetcher.load_data(sym, period="1d")

    total = len(risk_pcts) * len(STRATEGIES) * len(symbols)
    print(f"[Sweep] Running {total} backtests = "
          f"{len(risk_pcts)} risk_pcts × {len(STRATEGIES)} strategies × {len(symbols)} symbols")
    print(f"        risk_pcts (base) = {[f'{r:.1%}' for r in risk_pcts]}")
    print(f"        strategy scales = {STRATEGY_SCALE}")

    rows = []
    t_start = time.time()
    done = 0
    for base_rp in risk_pcts:
        cfg = build_risk_cfg(base_risk_cfg, base_rp)
        engine = BacktestEngine(cfg)
        for sn in STRATEGIES:
            cls = STRATEGY_REGISTRY[sn]
            strat_cfg = strategies_cfg.get(sn, {})
            per_rp = cfg["per_strategy_overrides"][sn]["single_trade_risk_pct"]
            for sym in symbols:
                done += 1
                data = data_cache.get(sym)
                if data is None or data.empty:
                    print(f"[{done}/{total}] base={base_rp:.1%} {sn} {sym}: NO_DATA")
                    continue
                t0 = time.time()
                try:
                    r = run_single(engine, cls, strat_cfg, data, sym)
                    r.update({
                        "base_risk_pct": base_rp,
                        "strategy_risk_pct": per_rp,
                        "strategy": sn,
                        "symbol": sym,
                        "status": "OK",
                    })
                    rows.append(r)
                    t1 = time.time()
                    cr = r["calmar_ratio"]
                    cr_s = f"{cr:.2f}" if cr is not None else "N/A"
                    print(f"[{done}/{total}] base={base_rp:.1%} per={per_rp:.2%} "
                          f"{sn:<10} {sym:<10} ret={r['total_return']:>7.2%} "
                          f"dd={r['max_drawdown']:>6.2%} calmar={cr_s} ({t1-t0:.1f}s)")
                except Exception as e:
                    rows.append({
                        "base_risk_pct": base_rp, "strategy_risk_pct": per_rp,
                        "strategy": sn, "symbol": sym, "status": f"ERR: {e}",
                    })
                    print(f"[{done}/{total}] ERR {sn} {sym}: {e}")

    total_time = time.time() - t_start
    print(f"\nAll done in {total_time:.1f}s ({total_time/total:.1f}s/run)")

    # === 输出 ===
    output_dir = os.path.join(project_root, args.output)
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"risk_pct_sweep_{ts}.csv")
    md_path = os.path.join(output_dir, f"risk_pct_sweep_{ts}.md")

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"✓ CSV -> {csv_path}")

    df_ok = df[df["status"] == "OK"].copy()

    # 聚合：按 (base_risk_pct, strategy) 对 9 标的取均值
    agg = df_ok.groupby(["base_risk_pct", "strategy"]).agg(
        avg_return=("total_return", "mean"),
        avg_dd=("max_drawdown", "mean"),
        worst_dd=("max_drawdown", "max"),
        avg_sharpe=("sharpe_ratio", "mean"),
        avg_calmar=("calmar_ratio", "mean"),
        avg_trades=("trade_count", "mean"),
    ).reset_index()

    # 硬过滤：worst_dd ≤ 15%（任何单一标的都不能越限）
    agg["pass_dd_limit"] = agg["worst_dd"] <= MAX_DD_LIMIT
    agg_pass = agg[agg["pass_dd_limit"]].sort_values("avg_calmar", ascending=False)

    # 整体（不分策略）的按 base_risk_pct 聚合
    overall = df_ok.groupby("base_risk_pct").agg(
        avg_return=("total_return", "mean"),
        avg_dd=("max_drawdown", "mean"),
        worst_dd=("max_drawdown", "max"),
        avg_sharpe=("sharpe_ratio", "mean"),
        avg_calmar=("calmar_ratio", "mean"),
    ).reset_index()
    overall["pass_dd_limit"] = overall["worst_dd"] <= MAX_DD_LIMIT

    # ========= Markdown 报告 =========
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 单笔风险预算扫描 - {ts}\n\n")
        f.write(f"- 标的: {len(symbols)} 只（{', '.join(symbols)}）\n")
        f.write(f"- 策略: {', '.join(STRATEGIES)}\n")
        f.write(f"- Risk Pct 档位: {[f'{r:.1%}' for r in risk_pcts]}\n")
        f.write(f"- 策略缩放因子: MA×{STRATEGY_SCALE['ma_cross']}, "
                f"RSI×{STRATEGY_SCALE['rsi']}, Mom×{STRATEGY_SCALE['momentum']}\n")
        f.write(f"- 硬约束: worst_MaxDD ≤ {MAX_DD_LIMIT:.0%}\n")
        f.write(f"- 总回测次数: {total}，耗时 {total_time:.1f}s\n\n")

        # 1) 整体档位对比
        f.write("## 1. 全局档位对比（9标的×3策略 均值）\n\n")
        f.write("| base_risk_pct | avg_return | avg_dd | worst_dd | avg_sharpe | avg_calmar | ≤15% |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for _, r in overall.iterrows():
            mark = "✅" if r["pass_dd_limit"] else "❌"
            f.write(f"| {r['base_risk_pct']:.1%} | {r['avg_return']:.2%} | "
                    f"{r['avg_dd']:.2%} | {r['worst_dd']:.2%} | "
                    f"{r['avg_sharpe']:.2f} | {r['avg_calmar']:.2f} | {mark} |\n")
        f.write("\n")

        # 2) 按策略×档位汇总
        f.write("## 2. 按策略×档位汇总\n\n")
        for sn in STRATEGIES:
            sub = agg[agg["strategy"] == sn].sort_values("base_risk_pct")
            f.write(f"### {sn}\n\n")
            f.write("| base_risk_pct | per_strategy_rp | avg_return | avg_dd | worst_dd | avg_sharpe | avg_calmar | ≤15% |\n")
            f.write("|---|---|---|---|---|---|---|---|\n")
            for _, r in sub.iterrows():
                mark = "✅" if r["pass_dd_limit"] else "❌"
                per_rp = r["base_risk_pct"] * STRATEGY_SCALE[sn]
                f.write(f"| {r['base_risk_pct']:.1%} | {per_rp:.2%} | "
                        f"{r['avg_return']:.2%} | {r['avg_dd']:.2%} | "
                        f"{r['worst_dd']:.2%} | {r['avg_sharpe']:.2f} | "
                        f"{r['avg_calmar']:.2f} | {mark} |\n")
            f.write("\n")

        # 3) MaxDD≤15% 过滤后的 Calmar 排行（推荐候选）
        f.write("## 3. 🏆 MaxDD ≤ 15% 约束下 Calmar Top 排行\n\n")
        if agg_pass.empty:
            f.write("⚠️ 没有任何 (档位×策略) 组合满足 worst_MaxDD ≤ 15%！\n")
            f.write("建议放宽约束到 20% 或降低最低档位。\n\n")
        else:
            f.write("| Rank | base_risk_pct | strategy | per_rp | avg_return | worst_dd | avg_calmar |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            for i, (_, r) in enumerate(agg_pass.head(10).iterrows(), 1):
                per_rp = r["base_risk_pct"] * STRATEGY_SCALE[r["strategy"]]
                f.write(f"| {i} | {r['base_risk_pct']:.1%} | {r['strategy']} | "
                        f"{per_rp:.2%} | {r['avg_return']:.2%} | "
                        f"{r['worst_dd']:.2%} | {r['avg_calmar']:.2f} |\n")
            f.write("\n")

        # 4) 推荐参数（按策略选 Calmar 最高且通过 DD 约束的档位）
        f.write("## 4. 📌 推荐参数（按策略分级）\n\n")
        f.write("| strategy | recommended base_rp | per_strategy_rp | avg_return | worst_dd | avg_calmar |\n")
        f.write("|---|---|---|---|---|---|\n")
        recommendations = {}
        for sn in STRATEGIES:
            sub = agg_pass[agg_pass["strategy"] == sn]
            if sub.empty:
                # 兜底：即使超 15% 也选 Calmar 最高的，标记为超限
                sub_all = agg[agg["strategy"] == sn].sort_values("avg_calmar", ascending=False)
                if not sub_all.empty:
                    r = sub_all.iloc[0]
                    per_rp = r["base_risk_pct"] * STRATEGY_SCALE[sn]
                    recommendations[sn] = {
                        "base_rp": r["base_risk_pct"],
                        "per_rp": per_rp,
                        "avg_return": r["avg_return"],
                        "worst_dd": r["worst_dd"],
                        "avg_calmar": r["avg_calmar"],
                        "pass": False,
                    }
                    f.write(f"| {sn} | {r['base_risk_pct']:.1%} ⚠️超限 | "
                            f"{per_rp:.2%} | {r['avg_return']:.2%} | "
                            f"{r['worst_dd']:.2%} | {r['avg_calmar']:.2f} |\n")
            else:
                r = sub.iloc[0]
                per_rp = r["base_risk_pct"] * STRATEGY_SCALE[sn]
                recommendations[sn] = {
                    "base_rp": r["base_risk_pct"],
                    "per_rp": per_rp,
                    "avg_return": r["avg_return"],
                    "worst_dd": r["worst_dd"],
                    "avg_calmar": r["avg_calmar"],
                    "pass": True,
                }
                f.write(f"| {sn} | {r['base_risk_pct']:.1%} ✅ | "
                        f"{per_rp:.2%} | {r['avg_return']:.2%} | "
                        f"{r['worst_dd']:.2%} | {r['avg_calmar']:.2f} |\n")

        f.write("\n### 建议改 config/risk.yaml 的配置\n\n")
        f.write("```yaml\nper_strategy_overrides:\n")
        for sn, rec in recommendations.items():
            f.write(f"  {sn}:\n")
            f.write(f"    single_trade_risk_pct: {rec['per_rp']:.4f}   "
                    f"# {rec['per_rp']:.2%} (from sweep, avg_calmar={rec['avg_calmar']:.2f})\n")
        f.write("```\n")

    print(f"✓ MD  -> {md_path}")
    return recommendations


if __name__ == "__main__":
    main()

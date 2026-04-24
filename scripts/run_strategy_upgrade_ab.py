#!/usr/bin/env python3
"""
Strategy Upgrade A/B Comparison - 策略确认层升级 A/B 对比（阶段 6）

目标：对比 MA Cross 和 RSI 两个策略在"启用确认层"vs"关闭确认层"下的效果：
- MA Cross: volume_confirm_enabled True vs False
- RSI: trend_filter_enabled True vs False
- Momentum 不动（作为对照组，保持原行为）

对比维度：
- 2 策略（ma_cross / rsi）× 2 配置（upgraded / legacy）× 9 标的 = 36 次回测
- 预估 ~3.5s/次 × 36 ≈ 2 分钟

输出：
- output/strategy_upgrade_ab_YYYYMMDD_HHMMSS.csv
- output/strategy_upgrade_ab_YYYYMMDD_HHMMSS.md
  - 升级前后指标对比表（年化/MaxDD/Calmar/胜率/交易次数/信号数）
  - Before/After Δ 聚合
  - 自动判断 A/B/C 路决策分叉
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


STRATEGIES = ["ma_cross", "rsi"]  # Momentum 不纳入本次 A/B
QUICK_SYMBOLS = ["AAPL.US", "MSFT.US", "NVDA.US"]


def build_strat_cfg(base_cfg: dict, strategy: str, upgraded: bool) -> dict:
    """根据 upgraded 开关构造策略配置。

    upgraded=True  → 启用确认层（新默认基线）
    upgraded=False → 关闭确认层（legacy 行为）
    """
    cfg = copy.deepcopy(base_cfg.get(strategy, {}))
    if strategy == "ma_cross":
        cfg["volume_confirm_enabled"] = upgraded
    elif strategy == "rsi":
        cfg["trend_filter_enabled"] = upgraded
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


def format_pct(v, digits=2):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    return f"{v*100:.{digits}f}%"


def format_ratio(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    return f"{v:.2f}"


def format_delta_pct(before, after):
    """差值以 pp 显示（百分点）"""
    if before is None or after is None or pd.isna(before) or pd.isna(after):
        return "N/A"
    diff = (after - before) * 100
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.2f}pp"


def format_delta_ratio(before, after):
    if before is None or after is None or pd.isna(before) or pd.isna(after):
        return "N/A"
    diff = after - before
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：仅 3 标的（AAPL/MSFT/NVDA）共 12 次回测")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    loader = ConfigLoader(os.path.join(project_root, "config"))
    base_risk_cfg = loader.get_risk_config()
    strategies_cfg = loader.get_strategies_config()

    watchlist = strategies_cfg.get("watchlist", [])
    symbols = QUICK_SYMBOLS if args.quick else watchlist

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))
    fetcher = DataFetcher(db=db, history_source="longport")

    # 缓存各标的数据
    data_cache = {}
    for sym in symbols:
        data_cache[sym] = fetcher.load_data(sym, period="1d")

    # 共用同一个 risk_cfg（阶段5 已确定最优参数）
    engine = BacktestEngine(base_risk_cfg)

    total = len(STRATEGIES) * 2 * len(symbols)  # 2 配置（upgraded/legacy）
    print(f"[A/B] Running {total} backtests = "
          f"{len(STRATEGIES)} strategies × 2 configs × {len(symbols)} symbols")
    print(f"      strategies: {STRATEGIES}")
    print(f"      configs: upgraded (confirm layer ON) / legacy (confirm layer OFF)")
    print()

    rows = []
    t_start = time.time()
    done = 0
    for sn in STRATEGIES:
        cls = STRATEGY_REGISTRY[sn]
        for upgraded in [False, True]:  # 先跑 legacy（基线），再跑 upgraded
            cfg_label = "upgraded" if upgraded else "legacy"
            strat_cfg = build_strat_cfg(strategies_cfg, sn, upgraded)
            for sym in symbols:
                done += 1
                data = data_cache.get(sym)
                if data is None or data.empty:
                    print(f"[{done}/{total}] {sn} {cfg_label} {sym}: NO_DATA")
                    continue
                t0 = time.time()
                try:
                    r = run_single(engine, cls, strat_cfg, data, sym)
                    r.update({
                        "strategy": sn,
                        "config": cfg_label,
                        "symbol": sym,
                        "status": "OK",
                    })
                    rows.append(r)
                    t1 = time.time()
                    cr = r["calmar_ratio"]
                    cr_s = f"{cr:.2f}" if cr is not None else "N/A"
                    print(f"[{done}/{total}] {sn:<10} {cfg_label:<9} {sym:<10} "
                          f"ret={r['total_return']:>7.2%} dd={r['max_drawdown']:>6.2%} "
                          f"calmar={cr_s} trades={r['trade_count']:>3} ({t1-t0:.1f}s)")
                except Exception as e:
                    rows.append({
                        "strategy": sn, "config": cfg_label,
                        "symbol": sym, "status": f"ERR: {e}",
                    })
                    print(f"[{done}/{total}] ERR {sn} {cfg_label} {sym}: {e}")

    total_time = time.time() - t_start
    print(f"\nAll done in {total_time:.1f}s ({total_time/total:.1f}s/run)")

    # === 输出 ===
    output_dir = os.path.join(project_root, args.output)
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"strategy_upgrade_ab_{ts}.csv")
    md_path = os.path.join(output_dir, f"strategy_upgrade_ab_{ts}.md")

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"✓ CSV -> {csv_path}")

    df_ok = df[df["status"] == "OK"].copy()

    # 按 (strategy, config) 聚合
    agg = df_ok.groupby(["strategy", "config"]).agg(
        avg_return=("total_return", "mean"),
        avg_annual=("annual_return", "mean"),
        avg_dd=("max_drawdown", "mean"),
        worst_dd=("max_drawdown", "max"),
        avg_sharpe=("sharpe_ratio", "mean"),
        avg_calmar=("calmar_ratio", "mean"),
        avg_win_rate=("win_rate", "mean"),
        avg_trades=("trade_count", "mean"),
    ).reset_index()

    # 透视成 Before/After 对比
    pivot_return = agg.pivot(index="strategy", columns="config", values="avg_return")
    pivot_annual = agg.pivot(index="strategy", columns="config", values="avg_annual")
    pivot_dd = agg.pivot(index="strategy", columns="config", values="avg_dd")
    pivot_worst_dd = agg.pivot(index="strategy", columns="config", values="worst_dd")
    pivot_calmar = agg.pivot(index="strategy", columns="config", values="avg_calmar")
    pivot_win = agg.pivot(index="strategy", columns="config", values="avg_win_rate")
    pivot_trades = agg.pivot(index="strategy", columns="config", values="avg_trades")

    # === 决策分叉逻辑 ===
    def judge_strategy(sn: str) -> dict:
        """对单策略判断升级是否有效"""
        try:
            ret_leg = pivot_annual.loc[sn, "legacy"]
            ret_up = pivot_annual.loc[sn, "upgraded"]
            win_leg = pivot_win.loc[sn, "legacy"]
            win_up = pivot_win.loc[sn, "upgraded"]
            dd_leg = pivot_worst_dd.loc[sn, "legacy"]
            dd_up = pivot_worst_dd.loc[sn, "upgraded"]

            ret_delta_pp = (ret_up - ret_leg) * 100
            win_delta_pp = (win_up - win_leg) * 100

            # 成功标准
            effective = (ret_delta_pp >= 2.0) or (win_delta_pp >= 5.0)
            # 退化警告（回撤变大，但收益/胜率没提升）
            dd_worse = dd_up > dd_leg + 0.02
            return {
                "strategy": sn,
                "effective": effective,
                "ret_delta_pp": ret_delta_pp,
                "win_delta_pp": win_delta_pp,
                "dd_worse": dd_worse,
            }
        except KeyError:
            return {"strategy": sn, "effective": False, "ret_delta_pp": 0,
                    "win_delta_pp": 0, "dd_worse": False}

    judges = [judge_strategy(sn) for sn in STRATEGIES]
    effective_count = sum(1 for j in judges if j["effective"])

    if effective_count == 2:
        path = "A"
        path_label = "A 路（全线升级成功）"
        recommendation = "两个策略升级均有效，建议采用新参数进入 Paper Trading 正式启动"
    elif effective_count == 1:
        eff_name = [j["strategy"] for j in judges if j["effective"]][0]
        path = "B"
        path_label = f"B 路（部分升级成功）"
        recommendation = (
            f"仅 {eff_name} 升级有效，建议采用该策略的新参数，"
            f"另一个策略回退到 legacy；下一轮规划 Momentum + ADX 组合"
        )
    else:
        path = "C"
        path_label = "C 路（升级不达预期）"
        recommendation = (
            "MA 量能确认和 RSI 趋势过滤效果均未达到年化 +2pp 或胜率 +5pp 门槛。"
            "建议两个策略都回退到 legacy 配置（yaml 里把 enabled 设为 false），"
            "先启动 Paper Trading 收集真实数据，同时规划多因子打分选股作为下一轮升级方向"
        )

    # ========= Markdown 报告 =========
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 策略确认层升级 A/B 对比 - {ts}\n\n")
        f.write(f"- 标的: {len(symbols)} 只（{', '.join(symbols)}）\n")
        f.write(f"- 策略: MA Cross, RSI（Momentum 本轮不动）\n")
        f.write(f"- 配置对比: `legacy`（关闭确认层）vs `upgraded`（启用确认层）\n")
        f.write(f"- 总回测次数: {total}, 用时: {total_time:.1f}s\n\n")

        # === 关键对比表 ===
        f.write("## 📊 升级前后对比（9标的均值）\n\n")
        f.write("| 策略 | 年化收益 | Δ | 最大回撤 worst | Δ | Calmar | Δ | 胜率 | Δ | 平均交易数 |\n")
        f.write("|------|---------|---|--------------|---|--------|---|------|---|----------|\n")
        for sn in STRATEGIES:
            try:
                a_leg = pivot_annual.loc[sn, "legacy"]
                a_up = pivot_annual.loc[sn, "upgraded"]
                dd_leg = pivot_worst_dd.loc[sn, "legacy"]
                dd_up = pivot_worst_dd.loc[sn, "upgraded"]
                c_leg = pivot_calmar.loc[sn, "legacy"]
                c_up = pivot_calmar.loc[sn, "upgraded"]
                w_leg = pivot_win.loc[sn, "legacy"]
                w_up = pivot_win.loc[sn, "upgraded"]
                t_leg = pivot_trades.loc[sn, "legacy"]
                t_up = pivot_trades.loc[sn, "upgraded"]

                f.write(
                    f"| **{sn}** | "
                    f"{format_pct(a_leg)} → {format_pct(a_up)} | "
                    f"{format_delta_pct(a_leg, a_up)} | "
                    f"{format_pct(dd_leg)} → {format_pct(dd_up)} | "
                    f"{format_delta_pct(dd_leg, dd_up)} | "
                    f"{format_ratio(c_leg)} → {format_ratio(c_up)} | "
                    f"{format_delta_ratio(c_leg, c_up)} | "
                    f"{format_pct(w_leg)} → {format_pct(w_up)} | "
                    f"{format_delta_pct(w_leg, w_up)} | "
                    f"{t_leg:.1f} → {t_up:.1f} |\n"
                )
            except KeyError as e:
                f.write(f"| **{sn}** | 数据缺失 ({e}) | - | - | - | - | - | - | - | - |\n")
        f.write("\n")

        # === 完整聚合表 ===
        f.write("## 📋 完整聚合指标\n\n")
        f.write("| strategy | config | avg_return | avg_annual | avg_dd | worst_dd | avg_sharpe | avg_calmar | avg_win_rate | avg_trades |\n")
        f.write("|----------|--------|-----------|-----------|--------|----------|-----------|-----------|-------------|-----------|\n")
        for _, row in agg.iterrows():
            calmar_s = f"{row['avg_calmar']:.4f}" if pd.notna(row['avg_calmar']) else "N/A"
            f.write(
                f"| {row['strategy']} | {row['config']} | "
                f"{row['avg_return']:.4f} | {row['avg_annual']:.4f} | "
                f"{row['avg_dd']:.4f} | {row['worst_dd']:.4f} | "
                f"{row['avg_sharpe']:.4f} | {calmar_s} | "
                f"{row['avg_win_rate']:.4f} | {row['avg_trades']:.2f} |\n"
            )
        f.write("\n")

        # === 各标的详情 ===
        f.write("## 📋 各标的回测详情\n\n")
        for sn in STRATEGIES:
            f.write(f"### {sn}\n\n")
            sub = df_ok[df_ok["strategy"] == sn].copy()
            # 透视：行=symbol，列=config，值=total_return / max_drawdown
            pv_ret = sub.pivot(index="symbol", columns="config", values="total_return")
            pv_dd = sub.pivot(index="symbol", columns="config", values="max_drawdown")
            pv_tr = sub.pivot(index="symbol", columns="config", values="trade_count")
            pv_win = sub.pivot(index="symbol", columns="config", values="win_rate")

            f.write("| 标的 | Return legacy | Return upgraded | Return Δ | MaxDD legacy | MaxDD upgraded | Trades legacy | Trades upgraded | WinRate Δ |\n")
            f.write("|------|--------------|----------------|---------|-------------|---------------|--------------|----------------|-----------|\n")
            for sym in pv_ret.index:
                try:
                    r_leg = pv_ret.loc[sym, "legacy"]
                    r_up = pv_ret.loc[sym, "upgraded"]
                    d_leg = pv_dd.loc[sym, "legacy"]
                    d_up = pv_dd.loc[sym, "upgraded"]
                    t_leg = pv_tr.loc[sym, "legacy"]
                    t_up = pv_tr.loc[sym, "upgraded"]
                    w_leg = pv_win.loc[sym, "legacy"]
                    w_up = pv_win.loc[sym, "upgraded"]
                    f.write(
                        f"| {sym} | {format_pct(r_leg)} | {format_pct(r_up)} | "
                        f"{format_delta_pct(r_leg, r_up)} | {format_pct(d_leg)} | "
                        f"{format_pct(d_up)} | {int(t_leg)} | {int(t_up)} | "
                        f"{format_delta_pct(w_leg, w_up)} |\n"
                    )
                except (KeyError, ValueError):
                    f.write(f"| {sym} | 数据不全 | - | - | - | - | - | - | - |\n")
            f.write("\n")

        # === 决策分叉 ===
        f.write("## 🎯 决策建议\n\n")
        f.write(f"**结论：{path_label}**\n\n")
        f.write(f"> {recommendation}\n\n")
        f.write("### 各策略判断明细\n\n")
        f.write("| 策略 | 年化收益 Δ | 胜率 Δ | 回撤恶化? | 升级是否有效 |\n")
        f.write("|------|----------|--------|---------|-----------|\n")
        for j in judges:
            f.write(
                f"| {j['strategy']} | {j['ret_delta_pp']:+.2f}pp | "
                f"{j['win_delta_pp']:+.2f}pp | {'⚠️是' if j['dd_worse'] else '否'} | "
                f"{'✅ 有效' if j['effective'] else '❌ 未达标'} |\n"
            )
        f.write("\n")
        f.write("**成功门槛**：年化 +2pp 或 胜率 +5pp（二选一）\n")

    print(f"✓ Markdown -> {md_path}")
    print()
    print(f"=== 决策分叉: {path_label} ===")
    print(recommendation)


if __name__ == "__main__":
    main()

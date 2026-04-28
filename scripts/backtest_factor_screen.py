#!/usr/bin/env python3
"""
阶段 9 V1 多因子回看回测

目的：验证因子打分的合理性。
- 读 DB 里的 Top-10 标的（V0 或 V1）
- 计算这 10 只标的过去 N 个交易日的等权组合累计收益
- 对比基准 QQQ 同期收益
- 输出 Alpha / Beta / 胜率 等指标

"回看"模式（Look-back）：
  因为快照日期 = 今天，未来 K 线还没有，所以用过去 30 天倒推：
  "如果 30 天前按这些标的等权买入，持有到今天能否跑赢 QQQ"。
  这是因子合理性的反向验证，不是严格意义的 forward backtest。

未来版本：Paper Trading 每天存快照，几个月后可做真正的 forward 回测。

用法：
    python scripts/backtest_factor_screen.py --version v1   # 回测 V1 Top-10
    python scripts/backtest_factor_screen.py --version v0   # 回测 V0 Top-10
    python scripts/backtest_factor_screen.py --top 5        # 改 Top-5
    python scripts/backtest_factor_screen.py --days 60      # 改窗口
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from loguru import logger

from src.data.database import DatabaseManager


def load_topn_symbols(
    db: DatabaseManager,
    version: str,
    snapshot_date: str,
    top_n: int,
) -> List[dict]:
    """从 factor_snapshots 读某版本某日期的 Top-N"""
    with db._get_conn() as conn:
        cur = conn.execute(
            """SELECT symbol, rank, total_score, sector, industry FROM factor_snapshots
               WHERE version = ? AND date = ?
               ORDER BY rank ASC LIMIT ?""",
            (version, snapshot_date, top_n),
        )
        return [dict(r) for r in cur.fetchall()]


def load_kline_for_period(
    db: DatabaseManager,
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """从 kline_data 读指定区间的日 K"""
    with db._get_conn() as conn:
        df = pd.read_sql_query(
            """SELECT date, close FROM kline_data
               WHERE symbol = ? AND period = '1d'
                 AND date(date) >= date(?) AND date(date) <= date(?)
               ORDER BY date ASC""",
            conn,
            params=(symbol, start_date, end_date),
        )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def compute_returns(close: pd.Series) -> pd.Series:
    """日收益率序列"""
    return close.pct_change().fillna(0)


def compute_portfolio_returns(
    db: DatabaseManager,
    symbols: List[str],
    start: str,
    end: str,
) -> Tuple[pd.Series, pd.DataFrame]:
    """
    返回 (组合日收益率序列, 每只个股的累计收益 DataFrame)。
    等权组合：每只权重 1/N。
    """
    individual = {}
    for sym in symbols:
        df = load_kline_for_period(db, sym, start, end)
        if df.empty:
            logger.warning(f"[Backtest] {sym} K 线为空，用 0% 填充")
            continue
        ret = compute_returns(df["close"])
        ret.index = df["date"]
        individual[sym] = ret

    if not individual:
        return pd.Series(dtype="float64"), pd.DataFrame()

    # 把所有收益对齐（按日期 outer join，缺失填 0）
    all_df = pd.DataFrame(individual).fillna(0.0)

    # 等权组合
    port_daily_ret = all_df.mean(axis=1)

    # 累计净值（以 1 为起点）
    individual_cum = (1 + all_df).cumprod() - 1

    return port_daily_ret, individual_cum


def compute_metrics(port_ret: pd.Series, bench_ret: pd.Series) -> dict:
    """计算 Alpha / Beta / 胜率 等"""
    if port_ret.empty or bench_ret.empty:
        return {}

    # 对齐
    df = pd.DataFrame({"p": port_ret, "b": bench_ret}).dropna()
    if df.empty:
        return {}

    p = df["p"]
    b = df["b"]

    # Beta = cov(p, b) / var(b)
    cov = p.cov(b)
    var_b = b.var()
    beta = cov / var_b if var_b > 1e-9 else 0.0

    # 累计收益
    port_total = (1 + p).prod() - 1
    bench_total = (1 + b).prod() - 1

    # Alpha（简化版，忽略无风险利率）
    alpha = port_total - beta * bench_total

    # 胜率：组合 > 基准的日数比例
    win_days = (p > b).sum()
    total_days = len(df)
    win_rate = win_days / total_days if total_days > 0 else 0.0

    # 年化波动率
    port_vol = p.std() * np.sqrt(252)
    bench_vol = b.std() * np.sqrt(252)

    # Sharpe（假定无风险利率 4%）
    rf_daily = 0.04 / 252
    sharpe = (p.mean() - rf_daily) / p.std() * np.sqrt(252) if p.std() > 1e-9 else 0.0

    # 最大回撤
    cum = (1 + p).cumprod()
    running_max = cum.cummax()
    drawdown = (cum - running_max) / running_max
    max_dd = drawdown.min()

    return {
        "port_total_return": port_total,
        "bench_total_return": bench_total,
        "alpha": alpha,
        "beta": beta,
        "win_rate": win_rate,
        "port_volatility": port_vol,
        "bench_volatility": bench_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "n_days": total_days,
    }


def render_report(
    version: str,
    snapshot_date: str,
    start: str,
    end: str,
    top_symbols: List[dict],
    metrics: dict,
    individual_cum: pd.DataFrame,
    port_ret: pd.Series,
    bench_ret: pd.Series,
    output_path: str,
):
    lines = []
    lines.append(f"# 📈 阶段 9 {version.upper()} Top-10 回看回测报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## 🎯 回测设置")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|:---|:---|")
    lines.append(f"| 打分版本 | {version.upper()} |")
    lines.append(f"| 快照日期 | {snapshot_date} |")
    lines.append(f"| 回测区间 | {start} → {end}（{metrics.get('n_days', 0)} 交易日）|")
    lines.append(f"| 组合 | {len(top_symbols)} 只等权（每只 {100/max(len(top_symbols),1):.0f}%）|")
    lines.append(f"| 基准 | QQQ.US |")
    lines.append("| 回测模式 | **Look-back（回看验证）** |")
    lines.append("")

    lines.append("> ⚠️ **回看模式说明**：由于因子快照日期 = 今天（2026-04-25），未来 K 线不存在。")
    lines.append("> 这里是**反向验证**——假设 30 天前按当前 Top-10 等权买入，持有到今天是否跑赢 QQQ。")
    lines.append("> 这是因子合理性的参考指标，不是严格意义的 forward backtest。")
    lines.append("> 未来 Paper Trading 积累 3 个月快照后，可做真正的前瞻回测。")
    lines.append("")

    lines.append("## 🏆 组合构成（Top-10）")
    lines.append("")
    lines.append("| 排名 | 标的 | 总分 | Sector | Industry |")
    lines.append("|:---:|:---|---:|:---|:---|")
    for s in top_symbols:
        lines.append(
            f"| #{s['rank']} | **{s['symbol']}** | {s['total_score']:+.2f} | "
            f"{s.get('sector') or '-'} | {s.get('industry') or '-'} |"
        )
    lines.append("")

    lines.append("## 📊 核心指标")
    lines.append("")
    port_ret_pct = metrics.get("port_total_return", 0) * 100
    bench_ret_pct = metrics.get("bench_total_return", 0) * 100
    excess_ret = port_ret_pct - bench_ret_pct
    alpha_pct = metrics.get("alpha", 0) * 100

    verdict_emoji = "🏆" if excess_ret > 0 else "📉"

    lines.append("| 指标 | 组合（Top-10）| QQQ 基准 | 差值 |")
    lines.append("|:---|---:|---:|---:|")
    lines.append(f"| 累计收益 | **{port_ret_pct:+.2f}%** | {bench_ret_pct:+.2f}% | **{excess_ret:+.2f}%** {verdict_emoji} |")
    lines.append(f"| 年化波动率 | {metrics.get('port_volatility', 0)*100:.2f}% | {metrics.get('bench_volatility', 0)*100:.2f}% | - |")
    lines.append(f"| Alpha（简化版）| {alpha_pct:+.2f}% | - | - |")
    lines.append(f"| Beta | {metrics.get('beta', 0):.3f} | 1.000 | - |")
    lines.append(f"| Sharpe（Rf=4%）| {metrics.get('sharpe', 0):.2f} | - | - |")
    lines.append(f"| 最大回撤 | {metrics.get('max_drawdown', 0)*100:.2f}% | - | - |")
    lines.append(f"| 跑赢基准天数 | {int(metrics.get('win_rate', 0) * metrics.get('n_days', 0))}/{metrics.get('n_days', 0)} ({metrics.get('win_rate', 0)*100:.1f}%) | - | - |")
    lines.append("")

    # === 结论判读 ===
    lines.append("## 💡 结论判读")
    lines.append("")
    if excess_ret > 5:
        lines.append(f"✅ **显著跑赢**：组合超额收益 **{excess_ret:+.2f}%**，说明 {version.upper()} 因子体系在过去 {metrics.get('n_days',0)} 交易日有显著选股能力。")
    elif excess_ret > 0:
        lines.append(f"✔️ **跑赢基准**：组合超额收益 {excess_ret:+.2f}%，小幅超过 QQQ。")
    elif excess_ret > -5:
        lines.append(f"⚠️ **小幅跑输**：组合跑输基准 {excess_ret:+.2f}%，{version.upper()} 因子在此窗口表现平平。")
    else:
        lines.append(f"❌ **显著跑输**：组合跑输基准 {excess_ret:+.2f}%，需要审视 {version.upper()} 因子权重或构成。")
    lines.append("")

    if metrics.get("beta", 0) > 1.3:
        lines.append(f"- 组合 Beta={metrics.get('beta'):.2f} 偏高，说明波动比 QQQ 大。")
    elif metrics.get("beta", 0) < 0.7:
        lines.append(f"- 组合 Beta={metrics.get('beta'):.2f} 偏低，说明波动比 QQQ 小（偏防御）。")
    lines.append("")

    # === 个股拆解 ===
    lines.append("## 🔍 个股拆解（区间累计收益）")
    lines.append("")
    if not individual_cum.empty:
        end_rets = individual_cum.iloc[-1] * 100
        end_rets_sorted = end_rets.sort_values(ascending=False)
        lines.append("| 排名 | 标的 | 区间收益 | 相对 QQQ |")
        lines.append("|:---:|:---|---:|---:|")
        bench_total = metrics.get("bench_total_return", 0) * 100
        for i, (sym, ret) in enumerate(end_rets_sorted.items(), 1):
            diff = ret - bench_total
            emoji = "🏆" if diff > 10 else ("✅" if diff > 0 else "❌")
            lines.append(f"| {i} | {sym} | {ret:+.2f}% | {diff:+.2f}% {emoji} |")
    lines.append("")

    # === 局限性 ===
    lines.append("## ⚠️ 局限性")
    lines.append("")
    lines.append("1. **回看模式不是 forward backtest**，纯反向验证因子是否\"挑中\"过去涨得好的标的")
    lines.append(f"2. {metrics.get('n_days', 0)} 交易日样本较小，结论仅供参考")
    lines.append("3. 等权组合未考虑真实交易成本、手续费、滑点")
    lines.append("4. 未考虑风控（止损/止盈/仓位）")
    lines.append("5. 样本选择偏差：当前 Top-10 可能包含 Momentum 强势股，自然倾向过去 30 天涨得好")
    lines.append("")
    lines.append("---")
    lines.append(f"_由 `scripts/backtest_factor_screen.py --version {version}` 生成_")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"回测报告已生成: {output_path}")


# ============================================================
# vs_holdings 模式：双组对比（V1 Top-N vs 当前持仓）
# ============================================================

def load_current_holdings_portfolio(
    db: DatabaseManager,
) -> Tuple[List[str], Dict[str, float]]:
    """读 trading_state.paper.positions，返回 (symbols, weights)。

    weights 按 market_value 加权（不算 cash）。
    返回：([symbols], {symbol: weight}) 其中 weights 之和 = 1.0。
    """
    from src.data.trading_state import TradingState

    state = TradingState(db.db_path)
    positions = state.get("paper.positions") or {}
    if not positions:
        return [], {}

    market_values = {}
    for sym, p in positions.items():
        mv = float(p.get("market_value", 0) or 0)
        if mv > 0:
            market_values[sym] = mv

    total = sum(market_values.values())
    if total <= 0:
        return [], {}

    weights = {s: v / total for s, v in market_values.items()}
    return list(market_values.keys()), weights


def compute_jaccard_overlap(set_a: set, set_b: set) -> float:
    """Jaccard 相似度 = |A ∩ B| / |A ∪ B|"""
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def render_vs_holdings_report(
    snapshot_date: str,
    start_date: str,
    end_date: str,
    v1_symbols: List[dict],   # V1 Top-N 详细信息
    holdings_symbols: List[str],
    holdings_weights: Dict[str, float],
    metrics_v1: dict,
    metrics_hold: dict,
    metrics_bench: dict,
    output_path: str,
    benchmark: str = "QQQ.US",
):
    """渲染双组对比 Markdown 报告"""
    v1_syms_set = {s["symbol"] for s in v1_symbols}
    hold_syms_set = set(holdings_symbols)

    overlap = v1_syms_set & hold_syms_set
    only_v1 = v1_syms_set - hold_syms_set
    only_hold = hold_syms_set - v1_syms_set
    jaccard = compute_jaccard_overlap(v1_syms_set, hold_syms_set)

    v1_ret = metrics_v1.get("port_total_return", 0) * 100
    hold_ret = metrics_hold.get("port_total_return", 0) * 100
    bench_ret = (metrics_bench.get("port_total_return", 0) * 100
                 if metrics_bench else 0.0)
    excess_v1 = v1_ret - hold_ret

    lines = []
    lines.append(f"# 📊 V1 Top-{len(v1_symbols)} vs 当前持仓 模拟换仓回测")
    lines.append("")
    lines.append(f"> 快照日期：`{snapshot_date}` · 回看区间：`{start_date}` → `{end_date}`")
    lines.append(f"> 模式：vs_holdings（仅分析，**不下任何单**）")
    lines.append("")

    # ==== 一、核心结论 ====
    lines.append("## 🎯 核心结论")
    lines.append("")
    if excess_v1 > 1.0:
        verdict = f"🏆 **V1 跑赢当前持仓 {excess_v1:+.2f}%**，值得考虑按 V1 Top 调仓"
    elif excess_v1 < -1.0:
        verdict = f"📉 **V1 跑输当前持仓 {excess_v1:+.2f}%**，当前持仓表现更好，不建议换仓"
    else:
        verdict = f"⚖️ **V1 与当前持仓基本持平**（差异 {excess_v1:+.2f}%），无明显换仓动机"
    lines.append(f"- {verdict}")
    lines.append(f"- 持仓重叠度（Jaccard）：**{jaccard:.0%}**（{len(overlap)} 只重叠 / {len(v1_syms_set | hold_syms_set)} 只总集）")
    lines.append(f"- 基准 {benchmark} 同期：{bench_ret:+.2f}%")
    lines.append("")

    # ==== 二、双组指标对比 ====
    lines.append("## 📈 双组指标对比")
    lines.append("")
    lines.append(f"| 指标 | V1 Top-{len(v1_symbols)} | 当前持仓 | 差异 |")
    lines.append("|---|---:|---:|---:|")

    def _fmt_pct(x): return f"{x*100:+.2f}%" if x is not None else "N/A"
    def _fmt_num(x, p=2): return f"{x:.{p}f}" if x is not None else "N/A"
    def _diff_pct(a, b): return f"{(a-b)*100:+.2f}%" if a is not None and b is not None else "—"

    rows = [
        ("累计收益", metrics_v1.get("port_total_return"), metrics_hold.get("port_total_return"), True),
        ("年化波动", metrics_v1.get("port_vol"), metrics_hold.get("port_vol"), True),
        ("Sharpe", metrics_v1.get("sharpe"), metrics_hold.get("sharpe"), False),
        ("最大回撤", metrics_v1.get("max_drawdown"), metrics_hold.get("max_drawdown"), True),
        ("Beta", metrics_v1.get("beta"), metrics_hold.get("beta"), False),
        ("Alpha", metrics_v1.get("alpha"), metrics_hold.get("alpha"), True),
        ("胜率", metrics_v1.get("win_rate"), metrics_hold.get("win_rate"), True),
    ]
    for name, va, vb, is_pct in rows:
        if is_pct:
            lines.append(f"| {name} | {_fmt_pct(va)} | {_fmt_pct(vb)} | {_diff_pct(va, vb)} |")
        else:
            d = (va - vb) if (va is not None and vb is not None) else None
            d_str = f"{d:+.2f}" if d is not None else "—"
            lines.append(f"| {name} | {_fmt_num(va)} | {_fmt_num(vb)} | {d_str} |")
    lines.append("")

    # ==== 三、持仓差异分析 ====
    lines.append("## 🔄 持仓差异分析")
    lines.append("")
    lines.append("### V1 推荐但当前未持有")
    if only_v1:
        v1_info = {s["symbol"]: s for s in v1_symbols}
        for sym in sorted(only_v1, key=lambda s: v1_info[s]["rank"]):
            info = v1_info[sym]
            lines.append(f"- **{sym}** · V1 排名 #{info['rank']} · "
                         f"score {info['total_score']:.2f} · "
                         f"{info.get('industry') or info.get('sector') or '?'}")
    else:
        lines.append("_无_")
    lines.append("")

    lines.append("### 当前持有但不在 V1 Top-N")
    if only_hold:
        for sym in sorted(only_hold):
            w = holdings_weights.get(sym, 0)
            lines.append(f"- **{sym}** · 当前权重 {w:.0%}")
    else:
        lines.append("_无_")
    lines.append("")

    lines.append("### 双方都有")
    if overlap:
        for sym in sorted(overlap):
            w = holdings_weights.get(sym, 0)
            lines.append(f"- **{sym}** · 持仓权重 {w:.0%}")
    else:
        lines.append("_无_")
    lines.append("")

    # ==== 四、调仓建议（仅展示，不下单）====
    lines.append("## 💡 调仓建议（仅参考，不会自动执行）")
    lines.append("")
    if jaccard >= 0.8:
        lines.append("> ✅ 重叠度高，**保持现有持仓即可**")
    elif excess_v1 > 2.0:
        lines.append(f"> 🟡 V1 显著占优（+{excess_v1:.2f}%），可考虑：")
        lines.append("> - 减仓"
                     f"：{', '.join(sorted(only_hold)) if only_hold else '无'}")
        lines.append("> - 加仓"
                     f"：{', '.join(sorted(only_v1)) if only_v1 else '无'}")
    elif excess_v1 < -2.0:
        lines.append(f"> 🔴 V1 显著跑输（{excess_v1:.2f}%），**不建议按 V1 调仓**")
    else:
        lines.append("> ⚖️ 差异不显著，**继续观察**，等数据更明确再决策")
    lines.append("")

    # ==== 五、当前持仓明细 ====
    lines.append("## 📦 当前持仓权重明细")
    lines.append("")
    lines.append("| 标的 | 权重 |")
    lines.append("|---|---:|")
    for sym, w in sorted(holdings_weights.items(), key=lambda x: -x[1]):
        lines.append(f"| {sym} | {w:.0%} |")
    lines.append("")

    # ==== 六、V1 Top-N 名单 ====
    lines.append(f"## 🎯 V1 Top-{len(v1_symbols)} 名单")
    lines.append("")
    lines.append("| Rank | 标的 | 总分 | 行业 |")
    lines.append("|---:|---|---:|---|")
    for s in v1_symbols:
        ind = s.get("industry") or s.get("sector") or "—"
        lines.append(f"| #{s['rank']} | **{s['symbol']}** | {s['total_score']:.2f} | {ind} |")
    lines.append("")

    lines.append("---")
    lines.append(
        f"_由 `scripts/backtest_factor_screen.py --mode vs_holdings` 生成 · "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}_"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"vs_holdings 报告已生成: {output_path}")


def run_vs_holdings_mode(args):
    """vs_holdings 模式：V1 Top-N vs 当前持仓双组对比"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot_date = args.date or datetime.now().strftime("%Y-%m-%d")

    end_dt = datetime.strptime(snapshot_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=int(args.days * 1.5))
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    logger.info(
        f"=== vs_holdings 模式 | V1 Top-{args.top} vs 当前持仓 | "
        f"快照 {snapshot_date} | 区间 {start_date} → {end_date} ==="
    )

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))

    # 1. 读 V1 Top-N
    v1_symbols = load_topn_symbols(db, "v1", snapshot_date, args.top)
    if not v1_symbols:
        logger.error(f"读不到 v1 @ {snapshot_date} 的 Top-{args.top}")
        logger.error("提示：先跑 `python scripts/run_factor_screen.py --version v1`")
        return 1
    v1_syms_list = [s["symbol"] for s in v1_symbols]
    logger.info(f"V1 Top-{args.top}: {v1_syms_list}")

    # 2. 读当前持仓
    hold_syms, hold_weights = load_current_holdings_portfolio(db)
    if not hold_syms:
        logger.error("当前无持仓数据，无法对比")
        return 1
    logger.info(f"当前持仓 {len(hold_syms)} 只：{hold_syms}")

    # 3. 双组组合收益（V1 等权 + 持仓按市值权重）
    v1_ret, _ = compute_portfolio_returns(db, v1_syms_list, start_date, end_date)
    hold_ret_series = _compute_weighted_portfolio_returns(
        db, hold_syms, hold_weights, start_date, end_date
    )

    if v1_ret.empty or hold_ret_series.empty:
        logger.error("组合收益为空（可能 K 线缺失）")
        return 1

    # 4. 基准
    bench_df = load_kline_for_period(db, args.benchmark, start_date, end_date)
    bench_ret = compute_returns(bench_df["close"]) if not bench_df.empty else pd.Series(dtype="float64")
    if not bench_df.empty:
        bench_ret.index = bench_df["date"]

    # 5. 计算指标（V1 vs Bench / Holdings vs Bench）
    metrics_v1 = compute_metrics(v1_ret, bench_ret) if not bench_ret.empty else {}
    metrics_hold = compute_metrics(hold_ret_series, bench_ret) if not bench_ret.empty else {}
    # 基准指标（自己 vs 自己 = 0 alpha 1 beta）
    metrics_bench = compute_metrics(bench_ret, bench_ret) if not bench_ret.empty else {}

    # 6. 渲染报告
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(
        output_dir, f"vs_holdings_{snapshot_date}.md"
    )
    render_vs_holdings_report(
        snapshot_date, start_date, end_date,
        v1_symbols, hold_syms, hold_weights,
        metrics_v1, metrics_hold, metrics_bench,
        output_path, args.benchmark,
    )

    # 7. 终端摘要
    v1_total = metrics_v1.get("port_total_return", 0) * 100
    hold_total = metrics_hold.get("port_total_return", 0) * 100
    excess = v1_total - hold_total
    print("\n" + "=" * 72)
    print(f"vs_holdings 回测总结 | {snapshot_date}")
    print("=" * 72)
    print(f"  V1 Top-{args.top} 累计收益:   {v1_total:+.2f}%")
    print(f"  当前持仓累计收益:        {hold_total:+.2f}%")
    print(f"  V1 - 持仓:              {excess:+.2f}%")
    print(f"  持仓重叠度:              {compute_jaccard_overlap(set(v1_syms_list), set(hold_syms)):.0%}")
    print(f"  报告路径:                {output_path}")
    print("=" * 72)
    return 0


def _compute_weighted_portfolio_returns(
    db: DatabaseManager,
    symbols: List[str],
    weights: Dict[str, float],
    start: str,
    end: str,
) -> pd.Series:
    """按权重计算组合日收益（不像 compute_portfolio_returns 是等权）"""
    individual = {}
    for sym in symbols:
        df = load_kline_for_period(db, sym, start, end)
        if df.empty:
            logger.warning(f"[Backtest] {sym} K 线为空")
            continue
        ret = compute_returns(df["close"])
        ret.index = df["date"]
        individual[sym] = ret

    if not individual:
        return pd.Series(dtype="float64")

    all_df = pd.DataFrame(individual).fillna(0.0)
    # 加权（缺数据的标的权重不剔除，影响很小，简化处理）
    weighted = sum(all_df[s] * weights.get(s, 0) for s in all_df.columns)
    return weighted


def main():
    parser = argparse.ArgumentParser(description="阶段 9 V1 多因子回看回测")
    parser.add_argument("--version", choices=["v0", "v1"], default="v1",
                        help="回测哪个版本的 Top-N（默认 v1）")
    parser.add_argument("--date", type=str, default=None, help="快照日期，默认今天")
    parser.add_argument("--top", type=int, default=10, help="Top-N，默认 10")
    parser.add_argument("--days", type=int, default=30, help="回看天数（交易日），默认 30")
    parser.add_argument("--benchmark", type=str, default="QQQ.US", help="基准")
    parser.add_argument(
        "--mode", choices=["standalone", "vs_holdings"], default="standalone",
        help="standalone=单组回测（默认）；vs_holdings=V1 Top-N vs 当前持仓双组对比",
    )
    args = parser.parse_args()

    if args.mode == "vs_holdings":
        return run_vs_holdings_mode(args)
    return run_standalone_mode(args)


def run_standalone_mode(args):
    """原有单组回测（保持向后兼容）"""

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot_date = args.date or datetime.now().strftime("%Y-%m-%d")

    # 计算回测区间（向前推 ~45 自然日 ≈ 30 交易日）
    end_dt = datetime.strptime(snapshot_date, "%Y-%m-%d")
    # 乘 1.5 是为了覆盖周末/假期
    start_dt = end_dt - timedelta(days=int(args.days * 1.5))
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    logger.info(
        f"=== 回测 {args.version.upper()} Top-{args.top} | "
        f"快照 {snapshot_date} | 区间 {start_date} → {end_date} ==="
    )

    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))

    # 1. 读 Top-N
    top_symbols = load_topn_symbols(db, args.version, snapshot_date, args.top)
    if not top_symbols:
        logger.error(f"读不到 {args.version} @ {snapshot_date} 的 Top-{args.top}")
        logger.error("提示：先跑 `python scripts/run_factor_screen.py --version v1` 生成快照")
        return 1
    logger.info(f"Top-{args.top}: {[s['symbol'] for s in top_symbols]}")

    # 2. 跑组合收益
    port_ret, individual_cum = compute_portfolio_returns(
        db, [s["symbol"] for s in top_symbols], start_date, end_date,
    )
    if port_ret.empty:
        logger.error("组合收益为空，可能 K 线缺失")
        return 1

    # 3. 基准收益
    bench_df = load_kline_for_period(db, args.benchmark, start_date, end_date)
    if bench_df.empty:
        logger.error(f"基准 {args.benchmark} K 线缺失")
        return 1
    bench_ret = compute_returns(bench_df["close"])
    bench_ret.index = bench_df["date"]

    # 4. 计算指标
    metrics = compute_metrics(port_ret, bench_ret)

    # 5. 渲染报告
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(
        output_dir, f"backtest_{args.version}_top{args.top}_{snapshot_date}.md"
    )
    render_report(
        args.version, snapshot_date, start_date, end_date,
        top_symbols, metrics, individual_cum, port_ret, bench_ret,
        output_path,
    )

    # 6. 终端打印
    port_ret_pct = metrics.get("port_total_return", 0) * 100
    bench_ret_pct = metrics.get("bench_total_return", 0) * 100
    excess = port_ret_pct - bench_ret_pct
    verdict = "🏆 跑赢" if excess > 0 else "📉 跑输"
    print("\n" + "=" * 72)
    print(f"回测总结 | {args.version.upper()} Top-{args.top} 组合")
    print("=" * 72)
    print(f"  组合累计收益:  {port_ret_pct:+.2f}%")
    print(f"  QQQ 累计收益:  {bench_ret_pct:+.2f}%")
    print(f"  超额收益:      {excess:+.2f}%  {verdict}")
    print(f"  Alpha（简化）: {metrics.get('alpha', 0)*100:+.2f}%")
    print(f"  Beta:          {metrics.get('beta', 0):.3f}")
    print(f"  Sharpe:        {metrics.get('sharpe', 0):.2f}")
    print(f"  最大回撤:      {metrics.get('max_drawdown', 0)*100:.2f}%")
    print(f"  跑赢天数:      {int(metrics.get('win_rate', 0) * metrics.get('n_days', 0))}/{metrics.get('n_days', 0)}")
    print()
    print(f"📝 详细报告: {output_path}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

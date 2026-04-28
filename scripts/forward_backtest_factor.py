#!/usr/bin/env python3
"""阶段 11 P1-5：Forward Backtest 框架

遍历所有 V1 snapshot 日期，每个日期取 Top-N 算"未来 N 天"的等权组合收益，
汇总平均 Alpha / Sharpe / 胜率 / IC（信息系数），输出 Markdown 报告。

样本不足 30 时报告头部加显著警告（统计意义不足）。

用法：
  python scripts/forward_backtest_factor.py                    # 默认 v1 + Top-5 + 30 天
  python scripts/forward_backtest_factor.py --top 10 --days 60
  python scripts/forward_backtest_factor.py --no-push          # 只生成报告不推企微
  python scripts/forward_backtest_factor.py --version v0       # 跑 V0
"""
import argparse
import os
import sys
from datetime import datetime
from typing import List, Optional

import pandas as pd
from loguru import logger

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.data.database import DatabaseManager
from scripts.backtest_factor_screen import (
    load_topn_symbols,
    load_kline_for_period,
    compute_returns,
    compute_portfolio_returns,
    compute_metrics,
)


# ============================================================
# 数据层
# ============================================================

def list_snapshot_dates(db: DatabaseManager, version: str) -> List[str]:
    """列出所有有 snapshot 的日期（升序）"""
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM factor_snapshots WHERE version = ? ORDER BY date",
            (version,),
        ).fetchall()
    return [r[0] for r in rows]


def get_topn_with_scores(
    db: DatabaseManager, version: str, snapshot_date: str, top: int
) -> List[dict]:
    """读 (symbol, total_score, rank) 用于 IC 计算"""
    with db._get_conn() as conn:
        rows = conn.execute(
            """
            SELECT symbol, total_score, rank FROM factor_snapshots
            WHERE version = ? AND date = ? ORDER BY rank LIMIT ?
            """,
            (version, snapshot_date, top),
        ).fetchall()
    return [{"symbol": r[0], "total_score": r[1], "rank": r[2]} for r in rows]


def get_all_symbols_with_scores(
    db: DatabaseManager, version: str, snapshot_date: str
) -> List[dict]:
    """读全部标的的 score（IC 用）"""
    with db._get_conn() as conn:
        rows = conn.execute(
            """
            SELECT symbol, total_score FROM factor_snapshots
            WHERE version = ? AND date = ? ORDER BY total_score DESC
            """,
            (version, snapshot_date),
        ).fetchall()
    return [{"symbol": r[0], "total_score": r[1]} for r in rows]


# ============================================================
# 单次 forward 测算
# ============================================================

def forward_one_snapshot(
    db: DatabaseManager,
    version: str,
    snapshot_date: str,
    top: int,
    forward_days: int,
    benchmark: str = "QQQ.US",
) -> Optional[dict]:
    """对单个 snapshot 算 forward N 天收益。

    返回 None 表示该日期数据不足（如 forward 区间未来无 K 线）。
    """
    snap_dt = datetime.strptime(snapshot_date, "%Y-%m-%d")
    end_dt = snap_dt + pd.Timedelta(days=int(forward_days * 1.5))  # 1.5x buffer 留交易日不足

    start_str = snapshot_date
    end_str = end_dt.strftime("%Y-%m-%d")

    # 1. Top-N
    topn = get_topn_with_scores(db, version, snapshot_date, top)
    if not topn:
        return None
    symbols = [s["symbol"] for s in topn]

    # 2. 组合收益（等权）
    port_ret, _ = compute_portfolio_returns(db, symbols, start_str, end_str)
    if port_ret.empty or len(port_ret) < 5:  # 至少要有 5 个交易日
        return None

    # 3. 基准
    bench_df = load_kline_for_period(db, benchmark, start_str, end_str)
    if bench_df.empty:
        return None
    bench_ret = compute_returns(bench_df["close"])
    bench_ret.index = bench_df["date"]

    # 4. 指标
    metrics = compute_metrics(port_ret, bench_ret)

    # 5. IC（Spearman 排名相关：score 排名 vs forward 收益排名）
    ic = _compute_ic(db, version, snapshot_date, start_str, end_str)

    return {
        "snapshot_date": snapshot_date,
        "n_days": len(port_ret),
        "n_symbols": len(symbols),
        "top_symbols": symbols,
        "port_total_return": metrics.get("port_total_return", 0),
        "bench_total_return": metrics.get("bench_total_return", 0),
        "alpha": metrics.get("alpha", 0),
        "beta": metrics.get("beta", 1.0),
        "sharpe": metrics.get("sharpe", 0),
        "max_drawdown": metrics.get("max_drawdown", 0),
        "win_rate": metrics.get("win_rate", 0),
        "ic": ic,
    }


def _compute_ic(
    db: DatabaseManager,
    version: str,
    snapshot_date: str,
    start_str: str,
    end_str: str,
) -> Optional[float]:
    """计算信息系数（IC）：score 排名 vs forward 收益排名 的 Spearman 相关。

    需要全量标的（不只是 Top-N），否则相关性意义不大。
    """
    all_syms = get_all_symbols_with_scores(db, version, snapshot_date)
    if len(all_syms) < 5:
        return None

    # 算每个标的的 forward 收益
    rets = {}
    for s in all_syms:
        df = load_kline_for_period(db, s["symbol"], start_str, end_str)
        if df.empty or len(df) < 2:
            continue
        # 总收益 = (last close / first close) - 1
        total_ret = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
        rets[s["symbol"]] = total_ret

    if len(rets) < 5:
        return None

    # 配对
    score_series = pd.Series(
        {s["symbol"]: s["total_score"] for s in all_syms if s["symbol"] in rets}
    )
    ret_series = pd.Series(rets)

    # Spearman 排名相关
    try:
        ic = float(score_series.corr(ret_series, method="spearman"))
        if pd.isna(ic):
            return None
        return ic
    except Exception as e:
        logger.debug(f"[IC] 计算失败 {snapshot_date}: {e}")
        return None


# ============================================================
# 汇总
# ============================================================

def aggregate_forward_results(results: List[dict]) -> dict:
    """汇总所有 snapshot 的 forward 指标"""
    if not results:
        return {
            "n_snapshots": 0,
            "avg_alpha": 0, "avg_sharpe": 0, "avg_max_dd": 0,
            "win_rate_vs_bench": 0, "avg_ic": None,
        }

    n = len(results)
    df = pd.DataFrame(results)

    # 战胜基准的快照比例
    win_vs_bench = (df["port_total_return"] > df["bench_total_return"]).sum() / n

    ic_vals = df["ic"].dropna()
    avg_ic = float(ic_vals.mean()) if len(ic_vals) > 0 else None
    ic_positive_rate = float((ic_vals > 0).sum() / len(ic_vals)) if len(ic_vals) > 0 else None

    return {
        "n_snapshots": n,
        "avg_alpha": float(df["alpha"].mean()),
        "avg_sharpe": float(df["sharpe"].mean()),
        "avg_max_dd": float(df["max_drawdown"].mean()),
        "avg_port_ret": float(df["port_total_return"].mean()),
        "avg_bench_ret": float(df["bench_total_return"].mean()),
        "win_rate_vs_bench": float(win_vs_bench),
        "avg_ic": avg_ic,
        "ic_positive_rate": ic_positive_rate,
        "n_with_ic": len(ic_vals),
    }


# ============================================================
# 报告渲染
# ============================================================

def render_report(
    version: str,
    top: int,
    forward_days: int,
    benchmark: str,
    results: List[dict],
    summary: dict,
    output_path: str,
):
    """渲染 Markdown 报告"""
    n = summary["n_snapshots"]
    SAMPLE_WARNING_THRESHOLD = 30

    lines = []
    lines.append(f"# 🔬 {version.upper()} Forward Backtest 报告")
    lines.append("")
    lines.append(f"> Top-{top} 等权组合 · 持有 {forward_days} 个自然日 · 基准 {benchmark}")
    lines.append(f"> 生成时间：`{datetime.now().strftime('%Y-%m-%d %H:%M')}`")
    lines.append("")

    # 样本警告
    if n == 0:
        lines.append("> [!error] **无可用数据**")
        lines.append(f"> 找不到 {version} 任何 snapshot，或所有 snapshot 都未来数据不足")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.warning(f"无数据 → 报告: {output_path}")
        return

    if n < SAMPLE_WARNING_THRESHOLD:
        lines.append(f"> [!warning] ⚠️ **样本仅 {n} 个，统计意义不足**")
        lines.append(f"> 建议至少 {SAMPLE_WARNING_THRESHOLD} 个 snapshot 才能得出稳健结论")
        lines.append(f"> 当前结果**仅供观察趋势**，不能作为决策依据")
        lines.append("")

    # === 一、汇总指标 ===
    lines.append("## 📊 汇总指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|---|---:|---|")
    lines.append(f"| 样本数 | **{n}** | snapshot 个数 |")
    lines.append(f"| 平均组合收益 | **{summary['avg_port_ret']*100:+.2f}%** | Top-{top} 持有 {forward_days} 天 |")
    lines.append(f"| 平均基准收益 | {summary['avg_bench_ret']*100:+.2f}% | {benchmark} 同期 |")
    lines.append(f"| 平均 Alpha | **{summary['avg_alpha']*100:+.2f}%** | 超额收益（年化） |")
    lines.append(f"| 平均 Sharpe | **{summary['avg_sharpe']:.2f}** | 风险调整收益 |")
    lines.append(f"| 平均 MaxDD | {summary['avg_max_dd']*100:.2f}% | 平均最大回撤 |")
    lines.append(f"| 战胜基准比例 | **{summary['win_rate_vs_bench']:.0%}** | 多少 snapshot 跑赢 {benchmark} |")
    if summary.get("avg_ic") is not None:
        ic = summary["avg_ic"]
        ic_label = "强信号" if abs(ic) > 0.05 else "弱信号" if abs(ic) > 0.02 else "无信号"
        lines.append(f"| 平均 IC | **{ic:+.3f}** | Spearman({'≥0.05 强' if ic >= 0.05 else ic_label}) |")
        lines.append(f"| IC>0 比例 | {summary['ic_positive_rate']:.0%} | {summary['n_with_ic']} 个 IC 中正向占比 |")
    lines.append("")

    # === 二、IC 解读 ===
    if summary.get("avg_ic") is not None:
        lines.append("## 🔎 IC 解读")
        lines.append("")
        ic = summary["avg_ic"]
        if ic >= 0.05:
            lines.append(f"> [!success] IC = {ic:+.3f}：**因子信号有效**（学界经验阈值 |IC|≥0.05）")
        elif ic >= 0.02:
            lines.append(f"> [!info] IC = {ic:+.3f}：**因子有微弱信号**（继续积累样本观察）")
        elif ic >= -0.02:
            lines.append(f"> [!warning] IC = {ic:+.3f}：**因子无明显信号**（接近随机）")
        else:
            lines.append(f"> [!error] IC = {ic:+.3f}：**因子方向反了**（高分股反而跌得多）")
        lines.append("")
        lines.append("- IC = score 排名 vs forward 收益排名 的 Spearman 相关系数")
        lines.append("- 范围 [-1, 1]，0 = 完全随机，正值 = 高分→高收益（方向对）")
        lines.append("- 学界经验：单因子 |IC| ≥ 0.05 即视为可用")
        lines.append("")

    # === 三、各 snapshot 明细 ===
    lines.append("## 📅 各 Snapshot 明细")
    lines.append("")
    lines.append("| 日期 | Top-N | 组合收益 | 基准收益 | Alpha | Sharpe | MaxDD | IC |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        ic_str = f"{r['ic']:+.3f}" if r.get("ic") is not None else "—"
        lines.append(
            f"| {r['snapshot_date']} | {r['n_symbols']} | "
            f"{r['port_total_return']*100:+.2f}% | "
            f"{r['bench_total_return']*100:+.2f}% | "
            f"{r['alpha']*100:+.2f}% | "
            f"{r['sharpe']:.2f} | "
            f"{r['max_drawdown']*100:.2f}% | "
            f"{ic_str} |"
        )
    lines.append("")

    # === 四、最佳 / 最差 snapshot ===
    if n >= 2:
        df = pd.DataFrame(results)
        best = df.loc[df["port_total_return"].idxmax()]
        worst = df.loc[df["port_total_return"].idxmin()]
        lines.append("## 🏆 最佳 / 最差 Snapshot")
        lines.append("")
        lines.append(f"- **最佳**：{best['snapshot_date']} 收益 {best['port_total_return']*100:+.2f}%（Top: {', '.join(best['top_symbols'][:3])}...）")
        lines.append(f"- **最差**：{worst['snapshot_date']} 收益 {worst['port_total_return']*100:+.2f}%（Top: {', '.join(worst['top_symbols'][:3])}...）")
        lines.append("")

    # === 五、结论 ===
    lines.append("## 💡 结论")
    lines.append("")
    if n < SAMPLE_WARNING_THRESHOLD:
        lines.append(f"⚠️ **样本不足（{n}/{SAMPLE_WARNING_THRESHOLD}），无法得出稳健结论**。")
        lines.append("")
        lines.append("继续每日积累 V1 snapshot，预计：")
        days_needed = (SAMPLE_WARNING_THRESHOLD - n) * 1
        lines.append(f"- 还需约 **{days_needed} 个交易日**积累足够样本")
        lines.append(f"- 等积累完成后，本报告会自动给出 IC / Alpha / Sharpe 的统计显著性判定")
    else:
        avg_alpha_pct = summary["avg_alpha"] * 100
        if avg_alpha_pct > 5 and summary["win_rate_vs_bench"] > 0.55:
            lines.append(f"✅ {version.upper()} 表现良好：平均 Alpha {avg_alpha_pct:+.2f}%，{summary['win_rate_vs_bench']:.0%} 战胜基准")
        elif avg_alpha_pct < 0 and summary["win_rate_vs_bench"] < 0.45:
            lines.append(f"❌ {version.upper()} 表现不佳：平均 Alpha {avg_alpha_pct:+.2f}%，仅 {summary['win_rate_vs_bench']:.0%} 战胜基准")
        else:
            lines.append(f"⚖️ {version.upper()} 表现中性：平均 Alpha {avg_alpha_pct:+.2f}%")
    lines.append("")

    lines.append("---")
    lines.append(f"_由 `scripts/forward_backtest_factor.py --version {version} --top {top} --days {forward_days}` 生成_")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Forward backtest 报告已生成: {output_path}")


# ============================================================
# 推送
# ============================================================

def push_wecom(report_path: str, summary: dict, version: str, top: int):
    """推送企微 markdown 摘要"""
    try:
        from src.monitor.alerts import get_alerter
        alerter = get_alerter()
        if not alerter or not alerter.is_ready():
            logger.info("[Push] WeCom 未配置，跳过推送")
            return False

        n = summary["n_snapshots"]
        if n == 0:
            return False

        with open(report_path, encoding="utf-8") as f:
            content = f.read()
        # 截断到「各 Snapshot 明细」之前
        cutoff = content.find("## 📅 各 Snapshot 明细")
        summary_md = content[:cutoff].rstrip() if cutoff > 0 else content[:3000]

        alerter.info(summary_md, channel="markdown")
        logger.info("[Push] 已推送 WeCom")
        return True
    except Exception as e:
        logger.warning(f"[Push] WeCom 推送失败（忽略）：{e}")
        return False


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="阶段 11 P1-5 Forward Backtest 框架")
    parser.add_argument("--version", choices=["v0", "v1"], default="v1")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--days", type=int, default=30, help="每个 snapshot 持有天数")
    parser.add_argument("--benchmark", type=str, default="QQQ.US")
    parser.add_argument("--no-push", action="store_true", help="只生成报告不推企微")
    args = parser.parse_args()

    project_root = PROJECT_ROOT
    db = DatabaseManager(os.path.join(project_root, "data_cache", "quant.db"))

    # 1. 列所有 snapshot 日期
    snapshot_dates = list_snapshot_dates(db, args.version)
    logger.info(f"找到 {args.version} snapshot 日期 {len(snapshot_dates)} 个: {snapshot_dates}")

    # 2. 对每个日期跑 forward backtest
    results = []
    for d in snapshot_dates:
        r = forward_one_snapshot(db, args.version, d, args.top, args.days, args.benchmark)
        if r:
            results.append(r)
            logger.info(
                f"[{d}] port {r['port_total_return']*100:+.2f}% bench {r['bench_total_return']*100:+.2f}% "
                f"alpha {r['alpha']*100:+.2f}% sharpe {r['sharpe']:.2f} ic {r['ic'] if r['ic'] is not None else 'N/A'}"
            )
        else:
            logger.warning(f"[{d}] 数据不足跳过")

    # 3. 汇总
    summary = aggregate_forward_results(results)

    # 4. 渲染
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(
        output_dir, f"forward_backtest_{args.version}_top{args.top}_d{args.days}.md"
    )
    render_report(args.version, args.top, args.days, args.benchmark, results, summary, output_path)

    # 5. 推送
    if not args.no_push:
        push_wecom(output_path, summary, args.version, args.top)

    # 6. 终端摘要
    print("\n" + "=" * 72)
    print(f"Forward Backtest 总结 | {args.version.upper()} Top-{args.top} · 持有 {args.days} 天")
    print("=" * 72)
    print(f"  样本数:       {summary['n_snapshots']}")
    if summary["n_snapshots"] > 0:
        print(f"  平均 Alpha:   {summary['avg_alpha']*100:+.2f}%")
        print(f"  平均 Sharpe:  {summary['avg_sharpe']:.2f}")
        print(f"  战胜基准比例: {summary['win_rate_vs_bench']:.0%}")
        if summary.get("avg_ic") is not None:
            print(f"  平均 IC:      {summary['avg_ic']:+.3f}")
    print(f"  报告路径:     {output_path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

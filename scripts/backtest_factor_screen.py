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
from typing import List, Tuple

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


def main():
    parser = argparse.ArgumentParser(description="阶段 9 V1 多因子回看回测")
    parser.add_argument("--version", choices=["v0", "v1"], default="v1",
                        help="回测哪个版本的 Top-N（默认 v1）")
    parser.add_argument("--date", type=str, default=None, help="快照日期，默认今天")
    parser.add_argument("--top", type=int, default=10, help="Top-N，默认 10")
    parser.add_argument("--days", type=int, default=30, help="回看天数（交易日），默认 30")
    parser.add_argument("--benchmark", type=str, default="QQQ.US", help="基准")
    args = parser.parse_args()

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

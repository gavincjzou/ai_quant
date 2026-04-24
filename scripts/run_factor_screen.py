#!/usr/bin/env python3
"""
阶段 9 多因子选股 V0 入口

功能：
1. 从 strategies.yaml 读 watchlist
2. 调 LongPort calc_indexes 拉 7 个原始指标
3. 4 因子 winsorized Z-Score 打分 + 加权聚合
4. 输出：
   - output/factor_screen_YYYY-MM-DD.csv（完整数据）
   - output/factor_screen_YYYY-MM-DD.md（Top-10 + Bottom-5 + 对照现持仓）
5. 存入 SQLite factor_snapshots 表

使用方法：
    python scripts/run_factor_screen.py                    # 用 watchlist + 今天日期
    python scripts/run_factor_screen.py --top 15          # 输出 Top 15
    python scripts/run_factor_screen.py --date 2026-04-25 # 指定日期
"""
import argparse
import os
import sys
from datetime import date, datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from src.data.database import DatabaseManager
from src.data.longport_client import LongPortClient
from src.data.trading_state import TradingState
from src.factor.factor_fetcher import FactorFetcher
from src.factor.factor_scorer import FactorScorer
from src.utils.config_loader import ConfigLoader


def load_current_positions(db: DatabaseManager) -> List[str]:
    """从 trading_state 读当前持仓标的列表"""
    try:
        ts = TradingState(db.db_path)
        positions = ts.get("paper.positions") or {}
        return list(positions.keys())
    except Exception as e:
        logger.warning(f"读持仓失败：{e}")
        return []


def load_per_symbol_map(strategies_cfg: dict) -> dict:
    """读 strategies.yaml 的 per_symbol_strategies 配置"""
    return strategies_cfg.get("per_symbol_strategies", {}) or {}


def render_markdown(
    scored_df,
    output_path: str,
    snapshot_date: str,
    top_n: int,
    current_positions: List[str],
    per_symbol_map: dict,
):
    """生成 Markdown 报告，含 Top-N + Bottom-5 + 对照现持仓"""
    from datetime import datetime as dt

    top = scored_df.head(top_n)
    bottom = scored_df.tail(5).iloc[::-1]  # 倒序，最差的在上面

    lines = []
    lines.append(f"# 📊 多因子选股 Top-{top_n} | {snapshot_date}")
    lines.append("")
    lines.append(f"> 生成时间：{dt.now().strftime('%Y-%m-%d %H:%M:%S')} · "
                 f"样本：{len(scored_df)} 只标的")
    lines.append("")

    # ---- 1. Top-N ----
    lines.append(f"## 🏆 Top-{top_n} 推荐标的\n")
    lines.append("| 排名 | 标的 | 总分 | Value | Momentum | Size | Liquidity | 持仓? | per_symbol |")
    lines.append("|:---:|:---|---:|---:|---:|---:|---:|:---:|:---|")
    for _, r in top.iterrows():
        sym = r["symbol"]
        in_pos = "✅" if sym in current_positions else ""
        mapped = per_symbol_map.get(sym, ["-"])
        mapped_str = ",".join(mapped) if isinstance(mapped, list) else str(mapped)
        lines.append(
            f"| #{int(r['rank'])} | **{sym}** | "
            f"{r['total_score']:+.2f} | "
            f"{r['value_score']:+.2f} | {r['momentum_score']:+.2f} | "
            f"{r['size_score']:+.2f} | {r['liquidity_score']:+.2f} | "
            f"{in_pos} | {mapped_str} |"
        )
    lines.append("")

    # ---- 2. 对照分析 ----
    lines.append("## 🔍 对照分析\n")

    # 现持仓在多因子里的排名
    held_scores = scored_df[scored_df["symbol"].isin(current_positions)].copy()
    lines.append(f"### 当前持仓 {len(current_positions)} 只在多因子中的表现\n")
    if held_scores.empty:
        lines.append("> 无持仓或持仓不在 watchlist")
    else:
        lines.append("| 排名 | 标的 | 总分 | 说明 |")
        lines.append("|:---:|:---|---:|:---|")
        for _, r in held_scores.iterrows():
            rank = int(r["rank"])
            sym = r["symbol"]
            score = r["total_score"]
            if rank <= top_n:
                note = "✅ 在 Top-" + str(top_n)
            elif rank <= len(scored_df) * 0.5:
                note = "⚠️ 排名中游"
            else:
                note = "❌ 多因子下表现弱，可考虑轮换"
            lines.append(f"| #{rank} | {sym} | {score:+.2f} | {note} |")
    lines.append("")

    # Top-N 中未持仓的（潜在加仓候选）
    top_syms = set(top["symbol"].tolist())
    pos_syms = set(current_positions)
    candidates = top_syms - pos_syms
    if candidates:
        cand_df = top[top["symbol"].isin(candidates)]
        lines.append(f"### 💡 Top-{top_n} 中当前未持仓（{len(candidates)} 只候选）\n")
        for _, r in cand_df.iterrows():
            mapped = per_symbol_map.get(r["symbol"], ["-"])
            mapped_str = ",".join(mapped) if isinstance(mapped, list) else str(mapped)
            lines.append(f"- **{r['symbol']}**（#{int(r['rank'])} 总分 {r['total_score']:+.2f}）"
                         f" · per_symbol 策略：{mapped_str}")
        lines.append("")

    # ---- 3. Bottom 5 对照 ----
    lines.append("## 👎 Bottom-5 最弱标的（供合理性检查）\n")
    lines.append("| 排名 | 标的 | 总分 | Value | Momentum | Size | Liquidity |")
    lines.append("|:---:|:---|---:|---:|---:|---:|---:|")
    for _, r in bottom.iterrows():
        lines.append(
            f"| #{int(r['rank'])} | {r['symbol']} | "
            f"{r['total_score']:+.2f} | "
            f"{r['value_score']:+.2f} | {r['momentum_score']:+.2f} | "
            f"{r['size_score']:+.2f} | {r['liquidity_score']:+.2f} |"
        )
    lines.append("")

    # ---- 4. 因子权重 + 方法说明 ----
    lines.append("## ⚙️ 打分方法\n")
    lines.append("- **因子权重**：Value 25% / Momentum 30% / Size 15% / Liquidity 30%")
    lines.append("- **归一化**：Winsorized Z-Score（5%-95% 分位数截断 + clip 到 [-3, 3]）")
    lines.append("- **Value**：低 PE（-）× 40% + 低 PB（-）× 40% + 高股息率（+）× 20%")
    lines.append("- **Momentum**：5 日涨幅 × 40% + 半年涨幅 × 60%")
    lines.append("- **Size**：log10(市值) 反向（偏向中小盘）")
    lines.append("- **Liquidity**：换手率反向（避免资金炒作的过热股）")
    lines.append("- **数据源**：LongPort `calc_indexes` 实时拉取（阶段 9 V0 无财报数据）")
    lines.append("")
    lines.append("---")
    lines.append(f"_由 `scripts/run_factor_screen.py` 生成 · 阶段 9 V0_")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Markdown 报告已生成: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="阶段 9 多因子选股 V0")
    parser.add_argument("--top", type=int, default=10, help="输出 Top-N，默认 10")
    parser.add_argument("--date", type=str, default=None, help="快照日期 YYYY-MM-DD")
    parser.add_argument("--symbols", type=str, default=None,
                        help="自定义标的列表（逗号分隔），不传用 watchlist")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    snapshot_date = args.date or date.today().isoformat()
    logger.info(f"=== 多因子选股 V0 | date={snapshot_date} ===")

    # 加载配置
    cfg_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_cfg = cfg_loader.get_strategies_config()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = strategies_cfg.get("watchlist", [])

    if not symbols:
        logger.error("无标的可扫描，退出")
        return
    logger.info(f"标的数量：{len(symbols)}")

    # 初始化组件
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)
    lp = LongPortClient()
    fetcher = FactorFetcher(lp, db)
    scorer = FactorScorer()

    # 1. 拉取原始指标
    logger.info("📡 拉取 LongPort calc_indexes...")
    raw_df = fetcher.fetch(symbols)
    logger.info(f"拉到 {len(raw_df)} 条原始数据")

    # 2. 打分 + 存 DB
    logger.info("🎯 计算四因子得分...")
    scored_df = scorer.score_and_store(raw_df, db, snapshot_date)

    # 3. 输出 CSV
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"factor_screen_{snapshot_date}.csv")
    scored_df.to_csv(csv_path, index=False, float_format="%.4f")
    logger.info(f"CSV 已保存: {csv_path}")

    # 4. 生成 Markdown 报告
    current_positions = load_current_positions(db)
    per_symbol_map = load_per_symbol_map(strategies_cfg)

    md_path = os.path.join(output_dir, f"factor_screen_{snapshot_date}.md")
    render_markdown(
        scored_df, md_path, snapshot_date, args.top,
        current_positions, per_symbol_map,
    )

    # 5. 打印 Top-N 到终端
    print("\n" + "=" * 70)
    print(f"🏆 多因子选股 Top-{args.top} | {snapshot_date}")
    print("=" * 70)
    cols = ["rank", "symbol", "total_score", "value_score",
            "momentum_score", "size_score", "liquidity_score"]
    print(scored_df[cols].head(args.top).to_string(index=False))
    print()
    print(f"📄 CSV:  {csv_path}")
    print(f"📝 报告: {md_path}")
    print()


if __name__ == "__main__":
    main()

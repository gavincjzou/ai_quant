#!/usr/bin/env python3
"""
阶段 9 多因子选股入口（支持 V0/V1 双版本）

功能：
1. 从 strategies.yaml 读 watchlist
2. V0：调 LongPort calc_indexes → 4 因子打分
   V1：LongPort + Westock 双数据源 → 6 因子打分（含 Quality + Industry + ETF 通道）
3. 输出：
   - output/factor_screen_YYYY-MM-DD[_v1].csv（完整数据）
   - output/factor_screen_YYYY-MM-DD[_v1].md（Top-N + Bottom-5 + 对照现持仓）
4. 存入 SQLite factor_snapshots 表（带 version 字段）

使用方法：
    python scripts/run_factor_screen.py                      # V1 默认
    python scripts/run_factor_screen.py --version v0         # 回归 V0 逻辑
    python scripts/run_factor_screen.py --version v1 --top 15
    python scripts/run_factor_screen.py --symbols NVDA.US,MSFT.US
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
from src.factor.v1_scorer import V1Scorer
from src.factor.industry_map import get_industry_bias
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
    return strategies_cfg.get("per_symbol_strategies", {}) or {}


# ============================================================
# V0 Markdown 渲染（保留原逻辑）
# ============================================================

def render_markdown_v0(
    scored_df, output_path, snapshot_date, top_n, current_positions, per_symbol_map,
):
    top = scored_df.head(top_n)
    bottom = scored_df.tail(5).iloc[::-1]

    lines = []
    lines.append(f"# 📊 多因子选股 V0 Top-{top_n} | {snapshot_date}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
                 f"样本：{len(scored_df)} 只标的 · 版本：V0（4 因子）")
    lines.append("")
    lines.append(f"## 🏆 Top-{top_n} 推荐标的\n")
    lines.append("| 排名 | 标的 | 总分 | Value | Momentum | Size | Liquidity | 持仓? |")
    lines.append("|:---:|:---|---:|---:|---:|---:|---:|:---:|")
    for _, r in top.iterrows():
        sym = r["symbol"]
        in_pos = "✅" if sym in current_positions else ""
        lines.append(
            f"| #{int(r['rank'])} | **{sym}** | {r['total_score']:+.2f} | "
            f"{r['value_score']:+.2f} | {r['momentum_score']:+.2f} | "
            f"{r['size_score']:+.2f} | {r['liquidity_score']:+.2f} | {in_pos} |"
        )
    lines.append("")
    lines.append("## 👎 Bottom-5\n")
    lines.append("| 排名 | 标的 | 总分 |")
    lines.append("|:---:|:---|---:|")
    for _, r in bottom.iterrows():
        lines.append(f"| #{int(r['rank'])} | {r['symbol']} | {r['total_score']:+.2f} |")
    lines.append("")
    lines.append("---")
    lines.append(f"_由 `scripts/run_factor_screen.py --version v0` 生成_")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"V0 Markdown 报告已生成: {output_path}")


# ============================================================
# V1 Markdown 渲染（六因子 + 对照 V0 + 行业分布）
# ============================================================

def load_v0_top10(db: DatabaseManager, snapshot_date: str) -> list:
    """从 DB 读 V0 Top-10 列表（factor_snapshots 里 version='v0'）"""
    try:
        with db._get_conn() as conn:
            cur = conn.execute(
                """SELECT symbol, rank, total_score FROM factor_snapshots
                   WHERE version = 'v0' AND date = ?
                   ORDER BY rank ASC LIMIT 10""",
                (snapshot_date,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"读 V0 Top-10 失败：{e}")
        return []


def render_markdown_v1(
    scored_df, output_path, snapshot_date, top_n, current_positions,
    per_symbol_map, v0_top10: list,
):
    top = scored_df.head(top_n)
    bottom = scored_df.tail(5).iloc[::-1]

    lines = []
    lines.append(f"# 📊 多因子选股 V1 Top-{top_n} | {snapshot_date}")
    lines.append("")
    lines.append(
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"样本：{len(scored_df)} 只标的 · 版本：**V1（六因子，含 Quality + Industry）**"
    )
    lines.append("")
    lines.append("> 🎯 **V1 相比 V0 的变化**：Value 25%→15% / Momentum 30%→35% / "
                 "Size 15%→5% / Liquidity 30%→15% / **新增 Quality 15% + Industry 15%** / "
                 "**ETF 走三因子特殊通道**（修 V0 换手率误伤 bug）")
    lines.append("")

    # === 1. Top-N（六因子完整展示）===
    lines.append(f"## 🏆 Top-{top_n} 推荐标的\n")
    lines.append(
        "| 排名 | 标的 | 总分 | **Quality** | **Industry** | Momentum | Value | Size | Liquidity | 持仓? | 策略 |"
    )
    lines.append("|:---:|:---|---:|---:|---:|---:|---:|---:|---:|:---:|:---|")
    for _, r in top.iterrows():
        sym = r["symbol"]
        in_pos = "✅" if sym in current_positions else ""
        mapped = per_symbol_map.get(sym, ["-"])
        mapped_str = ",".join(mapped) if isinstance(mapped, list) else str(mapped)
        lines.append(
            f"| #{int(r['rank'])} | **{sym}** | {r['total_score']:+.2f} | "
            f"{r['quality_score']:+.2f} | {r['industry_score']:+.2f} | "
            f"{r['momentum_score']:+.2f} | {r['value_score']:+.2f} | "
            f"{r['size_score']:+.2f} | {r['liquidity_score']:+.2f} | "
            f"{in_pos} | {mapped_str} |"
        )
    lines.append("")

    # === 2. V0 vs V1 对照 ===
    if v0_top10:
        lines.append("## 🔄 V0 vs V1 对照（Top-10 排名变化）\n")
        v1_top10 = top.head(10)
        v0_set = {x["symbol"] for x in v0_top10}
        v1_set = set(v1_top10["symbol"].tolist())

        lines.append("**V1 新进 Top-10**（V0 未入选）:")
        new_in_v1 = v1_set - v0_set
        if new_in_v1:
            for sym in sorted(new_in_v1):
                row = v1_top10[v1_top10["symbol"] == sym].iloc[0]
                lines.append(
                    f"- **{sym}** #{int(row['rank'])} 总分 {row['total_score']:+.2f} "
                    f"(Quality {row['quality_score']:+.2f} / Industry {row['industry_score']:+.2f})"
                )
        else:
            lines.append("- _无变化_")
        lines.append("")

        lines.append("**V0 掉出 Top-10**（V1 未入选）:")
        dropped = v0_set - v1_set
        if dropped:
            for sym in sorted(dropped):
                v1_row = scored_df[scored_df["symbol"] == sym]
                if not v1_row.empty:
                    r = v1_row.iloc[0]
                    lines.append(
                        f"- **{sym}** → V1 排名 #{int(r['rank'])}（V0 Top-10 → V1 掉出）"
                    )
        else:
            lines.append("- _无变化_")
        lines.append("")

    # === 3. 当前持仓在 V1 里的表现 ===
    held = scored_df[scored_df["symbol"].isin(current_positions)].copy()
    lines.append(f"## 💼 当前持仓 {len(current_positions)} 只在 V1 中的表现\n")
    if held.empty:
        lines.append("> 无持仓或持仓不在 watchlist")
    else:
        lines.append("| 排名 | 标的 | 总分 | Quality | Industry | 结论 |")
        lines.append("|:---:|:---|---:|---:|---:|:---|")
        for _, r in held.iterrows():
            rank = int(r["rank"])
            sym = r["symbol"]
            if rank <= top_n:
                note = "✅ 在 Top-" + str(top_n)
            elif rank <= len(scored_df) * 0.5:
                note = "⚠️ 中游"
            else:
                note = "❌ V1 下排名靠后"
            lines.append(
                f"| #{rank} | {sym} | {r['total_score']:+.2f} | "
                f"{r['quality_score']:+.2f} | {r['industry_score']:+.2f} | {note} |"
            )
    lines.append("")

    # === 4. 行业分布（Top-N）===
    lines.append(f"## 🏭 Top-{top_n} 行业分布\n")
    lines.append("| 标的 | Sector | Industry | Industry σ |")
    lines.append("|:---|:---|:---|---:|")
    for _, r in top.iterrows():
        sector = r.get("sector", "-") or "-"
        industry = r.get("industry", "-") or "-"
        lines.append(
            f"| {r['symbol']} | {sector} | {industry} | {r['industry_score']:+.2f} |"
        )
    lines.append("")

    # === 5. Bottom-5 ===
    lines.append("## 👎 Bottom-5 最弱标的\n")
    lines.append("| 排名 | 标的 | 总分 | Quality | Momentum | Value | 说明 |")
    lines.append("|:---:|:---|---:|---:|---:|---:|:---|")
    for _, r in bottom.iterrows():
        sym = r["symbol"]
        if sym in V1Scorer.ETF_SYMBOLS:
            note = "ETF（三因子通道）"
        elif r["quality_score"] < -0.5:
            note = "Quality 差"
        elif r["momentum_score"] < -0.5:
            note = "动量差"
        else:
            note = "-"
        lines.append(
            f"| #{int(r['rank'])} | {sym} | {r['total_score']:+.2f} | "
            f"{r['quality_score']:+.2f} | {r['momentum_score']:+.2f} | "
            f"{r['value_score']:+.2f} | {note} |"
        )
    lines.append("")

    # === 6. 方法说明 ===
    lines.append("## ⚙️ V1 打分方法\n")
    lines.append("### 因子权重")
    lines.append("| 因子 | 权重 | 子因子 |")
    lines.append("|:---|---:|:---|")
    lines.append("| **Value** | 15% | 低 PE(40%) + 低 PB(40%) + 高股息(20%) |")
    lines.append("| **Momentum** | 35% | 5日涨幅(40%) + 半年涨幅(60%) |")
    lines.append("| **Quality** | 15% | 净利率(30%) + 毛利率(20%) + 营业利润率(20%) + ROE(30%) |")
    lines.append("| **Size** | 5% | log10(市值) 反向 |")
    lines.append("| **Liquidity** | 15% | 换手率反向（降权修 ETF 误伤）|")
    lines.append("| **Industry** | 15% | 半导体 +1.0σ / AI基建 +0.8σ / 软件互联网 +0.7σ |")
    lines.append("")
    lines.append("### ETF 特殊通道")
    lines.append("SPY/QQQ/IWM 跳过 Quality/Liquidity/Industry，只走 "
                 "**Value 30% + Momentum 50% + Size 20%**（修 V0 ETF 换手率天然高被误伤的 bug）。")
    lines.append("")
    lines.append("### 数据源")
    lines.append("- **LongPort `calc_indexes`**：PE/PB/Dividend/市值/涨幅/换手率（实时）")
    lines.append("- **Westock（腾讯自选股 CLI skill）**：净利率/毛利率/ROE/sector/industry")
    lines.append("")
    lines.append("---")
    lines.append(f"_由 `scripts/run_factor_screen.py --version v1` 生成 · 阶段 9 V1_")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"V1 Markdown 报告已生成: {output_path}")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="阶段 9 多因子选股 V0/V1")
    parser.add_argument("--version", choices=["v0", "v1"], default="v1",
                        help="打分版本，默认 v1（V0 保留兼容）")
    parser.add_argument("--top", type=int, default=10, help="输出 Top-N，默认 10")
    parser.add_argument("--date", type=str, default=None, help="快照日期 YYYY-MM-DD")
    parser.add_argument("--symbols", type=str, default=None,
                        help="自定义标的（逗号分隔），不传用 watchlist")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot_date = args.date or date.today().isoformat()
    ver = args.version

    logger.info(f"=== 多因子选股 {ver.upper()} | date={snapshot_date} ===")

    # 配置
    cfg_loader = ConfigLoader(os.path.join(project_root, "config"))
    strategies_cfg = cfg_loader.get_strategies_config()
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = strategies_cfg.get("watchlist", [])
    if not symbols:
        logger.error("无标的可扫描，退出")
        return 1
    logger.info(f"标的数量：{len(symbols)}")

    # 组件
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)
    lp = LongPortClient()
    fetcher = FactorFetcher(lp, db)

    # ==== V0 ====
    if ver == "v0":
        scorer = FactorScorer()
        logger.info("📡 V0: 拉取 LongPort calc_indexes...")
        raw_df = fetcher.fetch(symbols)
        logger.info(f"拉到 {len(raw_df)} 条")
        scored_df = scorer.score_and_store(raw_df, db, snapshot_date)
        suffix = ""

    # ==== V1 ====
    else:
        scorer = V1Scorer()
        logger.info("📡 V1: 拉取 LongPort + Westock 合并数据...")
        raw_df = fetcher.fetch_v1(symbols)
        logger.info(f"拉到 {len(raw_df)} 条 × {len(raw_df.columns)} 字段")
        scored_df = scorer.score_and_store_v1(raw_df, db, snapshot_date)
        suffix = "_v1"

    # === 输出 ===
    output_dir = os.path.join(project_root, "output")
    os.makedirs(output_dir, exist_ok=True)

    csv_path = os.path.join(output_dir, f"factor_screen_{snapshot_date}{suffix}.csv")
    scored_df.to_csv(csv_path, index=False, float_format="%.4f")
    logger.info(f"CSV 已保存: {csv_path}")

    md_path = os.path.join(output_dir, f"factor_screen_{snapshot_date}{suffix}.md")
    current_positions = load_current_positions(db)
    per_symbol_map = load_per_symbol_map(strategies_cfg)

    if ver == "v0":
        render_markdown_v0(
            scored_df, md_path, snapshot_date, args.top,
            current_positions, per_symbol_map,
        )
    else:
        v0_top10 = load_v0_top10(db, snapshot_date)
        render_markdown_v1(
            scored_df, md_path, snapshot_date, args.top,
            current_positions, per_symbol_map, v0_top10,
        )

    # 终端打印
    print("\n" + "=" * 72)
    print(f"🏆 多因子选股 {ver.upper()} Top-{args.top} | {snapshot_date}")
    print("=" * 72)
    if ver == "v1":
        cols = ["rank", "symbol", "total_score", "quality_score",
                "industry_score", "momentum_score", "value_score"]
    else:
        cols = ["rank", "symbol", "total_score", "value_score",
                "momentum_score", "size_score", "liquidity_score"]
    print(scored_df[cols].head(args.top).to_string(index=False))
    print()
    print(f"📄 CSV:  {csv_path}")
    print(f"📝 报告: {md_path}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

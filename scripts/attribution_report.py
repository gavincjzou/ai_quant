"""
attribution_report.py - 业绩归因报告（阶段 11 P1-4）

按多个维度聚合分析交易表现：
- 按 strategy_name：哪个策略最赚钱、胜率、平均持仓时长
- 按 symbol：哪些标的赚钱最多、亏损最多
- 已实现 PnL（buy/sell 配对 FIFO）+ 持仓浮盈（buy + 当前价）

用法：
    python scripts/attribution_report.py                 # 全期间
    python scripts/attribution_report.py --days 30       # 近 30 天
    python scripts/attribution_report.py --no-push       # 只生成报告不推企微

集成：
    daily-scan 周一 hook 自动跑（同 weekly_rotation_report 节奏）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from src.data.database import DatabaseManager
from src.data.trading_state import TradingState


# ============================================================
# 数据加载
# ============================================================

def load_trades(db: DatabaseManager, since: Optional[datetime] = None) -> List[dict]:
    """加载 trade_records 表"""
    sql = """SELECT executed_at, symbol, side, quantity, price, commission,
                    strategy_name, signal_reason
             FROM trade_records
             WHERE 1=1"""
    params: list = []
    if since:
        sql += " AND executed_at >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY executed_at ASC"

    with db._get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def load_current_positions(db: DatabaseManager) -> Dict[str, dict]:
    """从 trading_state 读当前持仓"""
    state = TradingState(db.db_path)
    return state.get("paper.positions") or {}


# ============================================================
# PnL 计算（FIFO 配对 + 浮盈）
# ============================================================

def compute_realized_pnl_by_symbol(trades: List[dict]) -> Dict[str, dict]:
    """FIFO 配对计算每个 symbol 的已实现 PnL。

    返回 {symbol: {realized_pnl, trades_count, win_count, loss_count, by_strategy: {strat: pnl}}}
    """
    # 按 symbol 分组
    by_symbol: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        by_symbol[t["symbol"]].append(t)

    result = {}
    for sym, trade_list in by_symbol.items():
        # FIFO 队列
        queue: List[Tuple[float, int, str, dict]] = []  # (price, qty, strategy, raw_trade)
        realized = 0.0
        win = 0
        loss = 0
        by_strategy: Dict[str, float] = defaultdict(float)
        commission_sum = 0.0

        for t in trade_list:
            qty = int(t["quantity"])
            price = float(t["price"])
            commission = float(t.get("commission", 0) or 0)
            commission_sum += commission

            if t["side"].lower() == "buy":
                queue.append((price, qty, t.get("strategy_name", "unknown"), t))
            else:  # sell
                remaining_to_sell = qty
                while remaining_to_sell > 0 and queue:
                    buy_price, buy_qty, buy_strat, _ = queue[0]
                    matched = min(buy_qty, remaining_to_sell)
                    pnl = (price - buy_price) * matched - commission * (matched / qty)
                    realized += pnl
                    by_strategy[buy_strat] += pnl
                    if pnl > 0:
                        win += 1
                    else:
                        loss += 1

                    remaining_to_sell -= matched
                    if matched == buy_qty:
                        queue.pop(0)
                    else:
                        queue[0] = (buy_price, buy_qty - matched, buy_strat, _)

        # 剩余在 queue 里的就是当前持仓（不算已实现）
        result[sym] = {
            "realized_pnl": round(realized, 2),
            "trades_count": len(trade_list),
            "win_count": win,
            "loss_count": loss,
            "by_strategy": {k: round(v, 2) for k, v in by_strategy.items()},
            "commission_sum": round(commission_sum, 2),
        }
    return result


def compute_unrealized_pnl(positions: Dict[str, dict]) -> Dict[str, float]:
    """从 trading_state.paper.positions 读浮盈"""
    result = {}
    for sym, p in positions.items():
        result[sym] = round(float(p.get("unrealized_pnl", 0) or 0), 2)
    return result


def aggregate_by_strategy(
    realized_by_symbol: Dict[str, dict],
    trades: List[dict],
    positions: Dict[str, dict],
) -> Dict[str, dict]:
    """按 strategy 聚合 PnL / 胜率 / 交易数 / 浮盈"""
    # 已实现 PnL：从每 symbol 的 by_strategy 汇总
    realized_by_strat: Dict[str, dict] = defaultdict(
        lambda: {"realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
                 "unrealized_pnl": 0.0, "symbols": set()}
    )
    for sym, info in realized_by_symbol.items():
        for strat, pnl in info["by_strategy"].items():
            realized_by_strat[strat]["realized_pnl"] += pnl
        for strat in info["by_strategy"]:
            realized_by_strat[strat]["symbols"].add(sym)

    # 交易数 / 胜率：从原始 trades 算
    for t in trades:
        strat = t.get("strategy_name", "unknown")
        realized_by_strat[strat]["trades"] += 1

    # FIFO 没法直接算"按 strategy 的胜率"，简化用全部 sell 配对的胜率
    # 已在 by_strategy_pnl 体现，胜率近似用 win_count/total
    for sym, info in realized_by_symbol.items():
        # 胜率累计到该 sym 涉及的所有 strategy 上
        for strat in info["by_strategy"]:
            realized_by_strat[strat]["wins"] += info["win_count"]
            realized_by_strat[strat]["losses"] += info["loss_count"]

    # 浮盈：从持仓查（用持仓时记录的 strategy）
    # trading_state 持仓里没存 strategy_name，从 trade_records 找最近 buy
    pos_strat_map: Dict[str, str] = {}
    for t in reversed(trades):  # 倒序找最近 buy
        if t["side"].lower() == "buy" and t["symbol"] in positions and t["symbol"] not in pos_strat_map:
            pos_strat_map[t["symbol"]] = t.get("strategy_name", "unknown")

    for sym, p in positions.items():
        upnl = float(p.get("unrealized_pnl", 0) or 0)
        strat = pos_strat_map.get(sym, "unknown")
        realized_by_strat[strat]["unrealized_pnl"] += upnl
        realized_by_strat[strat]["symbols"].add(sym)

    # 转 dict + 计算总收益
    result = {}
    for strat, d in realized_by_strat.items():
        total_pnl = d["realized_pnl"] + d["unrealized_pnl"]
        win_rate = d["wins"] / max(d["wins"] + d["losses"], 1)
        result[strat] = {
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": round(d["realized_pnl"], 2),
            "unrealized_pnl": round(d["unrealized_pnl"], 2),
            "trades": d["trades"],
            "wins": d["wins"],
            "losses": d["losses"],
            "win_rate": win_rate,
            "symbols": sorted(d["symbols"]),
        }
    return result


# ============================================================
# Markdown 渲染
# ============================================================

def render_report(
    realized_by_symbol: Dict[str, dict],
    by_strategy: Dict[str, dict],
    unrealized_by_symbol: Dict[str, float],
    positions: Dict[str, dict],
    trades_count: int,
    period_str: str,
    output_path: str,
) -> None:
    """生成 Markdown 报告"""
    lines = []
    lines.append(f"# 📈 业绩归因报告 · {period_str}")
    lines.append("")
    lines.append(f"> 数据期间：{period_str} · 总交易数：{trades_count} 笔 · 当前持仓：{len(positions)} 只")
    lines.append("")

    # ==== 一、整体战绩 ====
    total_realized = sum(d["realized_pnl"] for d in realized_by_symbol.values())
    total_unrealized = sum(unrealized_by_symbol.values())
    total_pnl = total_realized + total_unrealized
    total_commission = sum(d["commission_sum"] for d in realized_by_symbol.values())

    lines.append("## 🎯 整体战绩")
    lines.append("")
    lines.append("| 项 | 金额 |")
    lines.append("|---|---:|")
    lines.append(f"| 已实现 PnL | ${total_realized:+,.2f} |")
    lines.append(f"| 浮动盈亏 | ${total_unrealized:+,.2f} |")
    lines.append(f"| **总盈亏** | **${total_pnl:+,.2f}** |")
    lines.append(f"| 累计手续费 | ${total_commission:,.2f} |")
    lines.append("")

    # ==== 二、按策略归因 ====
    lines.append("## 🤖 按策略归因（Per Strategy）")
    lines.append("")
    if by_strategy:
        lines.append("| 策略 | 总 PnL | 已实现 | 浮动 | 交易数 | 胜率 | 涉及标的 |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        # 按总 PnL 降序
        sorted_strats = sorted(by_strategy.items(), key=lambda x: -x[1]["total_pnl"])
        for strat, d in sorted_strats:
            symbols_str = ", ".join(d["symbols"][:5])
            if len(d["symbols"]) > 5:
                symbols_str += f" 等 {len(d['symbols'])}"
            lines.append(
                f"| {strat} | ${d['total_pnl']:+,.2f} | ${d['realized_pnl']:+,.2f} | "
                f"${d['unrealized_pnl']:+,.2f} | {d['trades']} | {d['win_rate']:.0%} | "
                f"{symbols_str} |"
            )
    else:
        lines.append("_无数据_")
    lines.append("")

    # ==== 三、按 symbol Top-10 ====
    lines.append("## 🏆 按标的 Top-10（按总 PnL 排序）")
    lines.append("")
    # 合并已实现 + 浮动
    all_symbols = set(realized_by_symbol.keys()) | set(unrealized_by_symbol.keys())
    sym_list = []
    for sym in all_symbols:
        realized = realized_by_symbol.get(sym, {}).get("realized_pnl", 0)
        unrealized = unrealized_by_symbol.get(sym, 0)
        sym_list.append({
            "symbol": sym,
            "realized": realized,
            "unrealized": unrealized,
            "total": realized + unrealized,
            "trades": realized_by_symbol.get(sym, {}).get("trades_count", 0),
        })
    sym_list.sort(key=lambda x: -x["total"])

    lines.append("| 标的 | 总 PnL | 已实现 | 浮动 | 交易数 |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in sym_list[:10]:
        emoji = "🟢" if s["total"] > 0 else ("🔴" if s["total"] < 0 else "⚪")
        lines.append(
            f"| {emoji} **{s['symbol']}** | ${s['total']:+,.2f} | "
            f"${s['realized']:+,.2f} | ${s['unrealized']:+,.2f} | {s['trades']} |"
        )
    lines.append("")

    # ==== 四、亏损 Top-3（如有）====
    losers = [s for s in sym_list if s["total"] < 0][:3]
    if losers:
        lines.append("## ⚠️ 亏损 Top-3（关注）")
        lines.append("")
        lines.append("| 标的 | 总亏损 | 交易数 |")
        lines.append("|---|---:|---:|")
        for s in losers:
            lines.append(f"| 🔴 **{s['symbol']}** | ${s['total']:+,.2f} | {s['trades']} |")
        lines.append("")

    # ==== 五、当前持仓明细 ====
    lines.append("## 📦 当前持仓浮盈明细")
    lines.append("")
    if positions:
        lines.append("| 标的 | 数量 | 现价 | 成本 | 浮盈 | 浮盈% |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for sym, p in sorted(positions.items()):
            qty = int(p.get("quantity", 0))
            cur = float(p.get("current_price", 0))
            cost = float(p.get("avg_cost", 0))
            upnl = float(p.get("unrealized_pnl", 0))
            pct = upnl / (cost * qty) if cost * qty > 0 else 0
            emoji = "🟢" if upnl > 0 else ("🔴" if upnl < -0.5 else "⚪")
            lines.append(
                f"| {emoji} {sym} | {qty} | ${cur:.2f} | ${cost:.2f} | "
                f"${upnl:+,.2f} | {pct:+.2%} |"
            )
    else:
        lines.append("_无持仓_")
    lines.append("")

    lines.append("---")
    lines.append(f"_由 `scripts/attribution_report.py` 生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"业绩归因报告已生成: {output_path}")


def build_wecom_summary(report_path: str, period_str: str) -> str:
    """企微推送摘要"""
    if not os.path.exists(report_path):
        return f"⚠️ 报告文件不存在: {report_path}"
    content = Path(report_path).read_text(encoding="utf-8")
    cutoff = content.find("## 📦 当前持仓浮盈明细")
    summary = content[:cutoff].strip() if cutoff > 0 else content[:2000]
    if len(summary) > 3500:
        summary = summary[:3500] + "\n\n_..._（详见完整报告）"
    summary += f"\n\n---\n📂 完整报告：`{os.path.basename(report_path)}`"
    return summary


def push_wecom(markdown_text: str, period_str: str) -> bool:
    """推企微"""
    try:
        from src.monitor.alerts import get_alerter
        alerter = get_alerter()
        if not getattr(alerter, "_wecom_enabled", False):
            logger.info("[Attribution] WeCom 未配置，跳过推送")
            return False
        title = f"业绩归因周报 · {period_str}"
        alerter.info(
            f"{title}（详见 markdown）",
            title=title,
            tags=["attribution", "weekly"],
            markdown=markdown_text,
        )
        logger.info("[Attribution] WeCom 推送完成")
        return True
    except Exception as e:
        logger.warning(f"[Attribution] WeCom 推送失败（忽略）: {e}")
        return False


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="业绩归因报告")
    parser.add_argument("--days", type=int, default=0,
                        help="近 N 天（0 = 全期间，默认 0）")
    parser.add_argument("--no-push", action="store_true",
                        help="只生成报告不推企微")
    args = parser.parse_args()

    db = DatabaseManager(os.path.join(_PROJECT_ROOT, "data_cache", "quant.db"))

    since = None
    if args.days > 0:
        since = datetime.now() - timedelta(days=args.days)
        period_str = f"近 {args.days} 天"
    else:
        period_str = "全期间"

    trades = load_trades(db, since)
    if not trades:
        logger.warning("[Attribution] 无交易数据可分析")
        return 1
    positions = load_current_positions(db)

    realized_by_symbol = compute_realized_pnl_by_symbol(trades)
    unrealized_by_symbol = compute_unrealized_pnl(positions)
    by_strategy = aggregate_by_strategy(realized_by_symbol, trades, positions)

    output_dir = os.path.join(_PROJECT_ROOT, "output")
    os.makedirs(output_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(output_dir, f"attribution_{today_str}.md")

    render_report(
        realized_by_symbol, by_strategy, unrealized_by_symbol, positions,
        len(trades), period_str, output_path,
    )

    if not args.no_push:
        summary = build_wecom_summary(output_path, period_str)
        push_wecom(summary, period_str)

    # 终端摘要
    total_realized = sum(d["realized_pnl"] for d in realized_by_symbol.values())
    total_unrealized = sum(unrealized_by_symbol.values())
    print("\n" + "=" * 72)
    print(f"✅ 业绩归因完成 · {period_str}")
    print(f"   交易数:          {len(trades)} 笔")
    print(f"   已实现 PnL:      ${total_realized:+,.2f}")
    print(f"   浮动盈亏:        ${total_unrealized:+,.2f}")
    print(f"   总盈亏:          ${total_realized + total_unrealized:+,.2f}")
    print(f"   按策略归因:      {len(by_strategy)} 个策略")
    print(f"   报告:            {output_path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

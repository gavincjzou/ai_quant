#!/usr/bin/env python3
"""
Dashboard HTML 生成器（阶段8 B1）

从 SQLite 聚合所有 Paper Trading 数据，生成单文件 HTML 网页。
- 完全离线（Chart.js 走 CDN，但首次加载后浏览器可缓存）
- 涨红跌绿（中国习惯）
- 6 个视图：净值曲线 / 持仓概览 / 策略表现 / 交易时间线 / ATR 止损 / 对账异常

用法：
    python scripts/build_dashboard.py
    python scripts/build_dashboard.py --output ~/Desktop/quant_dashboard.html
    python scripts/build_dashboard.py --open    # 生成后自动打开浏览器

输出：默认 ~/ai_quant/output/dashboard.html
"""

import argparse
import json
import os
import sqlite3
import sys
import webbrowser
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.data.database import DatabaseManager
from src.data.trading_state import TradingState
from src.utils.config_loader import ConfigLoader


# ============================================================
# 数据聚合层
# ============================================================

def collect_dashboard_data(project_root: str) -> Dict[str, Any]:
    """从 SQLite 聚合所有数据，返回 dashboard 渲染所需的 dict"""
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)
    state = TradingState(db_path)

    data: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": project_root,
    }

    # 1. 账户信息（从 trading_state.paper.account）
    acct = state.get("paper.account") or {}
    positions_raw = state.get("paper.positions") or {}
    sl_raw = state.get("paper.stop_loss") or {}
    risk_raw = state.get("paper.risk_state") or {}

    initial_capital = float(acct.get("initial_capital", 800000))
    cash = float(acct.get("cash", initial_capital))

    market_value = sum(
        float(p.get("market_value", 0)) for p in positions_raw.values()
    )
    total_assets = cash + market_value

    data["account"] = {
        "initial_capital": initial_capital,
        "cash": cash,
        "market_value": market_value,
        "total_assets": total_assets,
        "return_pct": (total_assets - initial_capital) / initial_capital,
        "last_saved_at": acct.get("last_saved_at"),
        "last_scan_date": state.get("paper.last_scan_date"),
    }

    # 2. 当前持仓
    positions_list = []
    for sym, p in positions_raw.items():
        qty = int(p.get("quantity", 0))
        if qty <= 0:
            continue
        avg_cost = float(p.get("avg_cost", 0))
        cur = float(p.get("current_price", avg_cost))
        mv = float(p.get("market_value", cur * qty))
        pnl = (cur - avg_cost) * qty
        pnl_pct = (cur - avg_cost) / avg_cost if avg_cost > 0 else 0
        positions_list.append({
            "symbol": sym,
            "quantity": qty,
            "avg_cost": avg_cost,
            "current_price": cur,
            "market_value": mv,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "weight": mv / max(total_assets, 1),
        })
    positions_list.sort(key=lambda x: x["market_value"], reverse=True)
    data["positions"] = positions_list

    # 3. ATR 止损追踪
    sl_positions = sl_raw.get("positions", {}) if isinstance(sl_raw, dict) else {}
    sl_list = []
    for sym, p in sl_positions.items():
        sl_list.append({
            "symbol": sym,
            "strategy_name": p.get("strategy_name"),
            "avg_cost": float(p.get("avg_cost", 0)),
            "current_price": float(p.get("current_price", 0)),
            "highest_price": float(p.get("highest_price", 0)),
            "current_stop": p.get("current_stop"),
            "tp1_price": p.get("tp1_price"),
            "tp2_price": p.get("tp2_price"),
            "tp3_price": p.get("tp3_price"),
            "tp1_triggered": bool(p.get("tp1_triggered", False)),
            "tp2_triggered": bool(p.get("tp2_triggered", False)),
            "tp3_triggered": bool(p.get("tp3_triggered", False)),
            "stop_moved_to_breakeven": bool(p.get("stop_moved_to_breakeven", False)),
            "remaining_size": int(p.get("remaining_size", 0)),
            # 计算到止损/TP 的距离（百分比）
            "distance_to_stop": (
                (p.get("current_stop") - float(p.get("current_price", 0)))
                / float(p.get("current_price", 1)) * 100
                if p.get("current_stop") else None
            ),
            "distance_to_tp1": (
                (p.get("tp1_price") - float(p.get("current_price", 0)))
                / float(p.get("current_price", 1)) * 100
                if p.get("tp1_price") else None
            ),
        })
    data["stop_loss"] = sl_list

    # 4. 检测对账异常（持仓 vs SL 一致性）
    pos_syms = set(p["symbol"] for p in positions_list)
    sl_syms = set(p["symbol"] for p in sl_list)
    issues = []
    for sym in pos_syms - sl_syms:
        issues.append({
            "level": "WARNING",
            "code": "SL_MISSING",
            "msg": f"{sym} 有持仓但 ATR 未追踪（无止损保护）",
        })
    for sym in sl_syms - pos_syms:
        issues.append({
            "level": "WARNING",
            "code": "SL_STALE",
            "msg": f"{sym} 已平仓但 SL 仍在追踪",
        })
    if risk_raw.get("is_circuit_breaker"):
        issues.append({
            "level": "CRITICAL",
            "code": "HALTED",
            "msg": "组合熔断中（回撤超阈值）",
        })
    data["issues"] = issues

    # 5. 交易记录（trade_records）
    trades_df = db.load_trades(trade_mode="paper")
    trades = []
    if trades_df is not None and not trades_df.empty:
        for _, r in trades_df.iterrows():
            trades.append({
                "id": int(r["id"]),
                "order_id": r.get("order_id"),
                "symbol": r["symbol"],
                "side": r["side"],
                "quantity": int(r["quantity"]),
                "price": float(r["price"]),
                "commission": float(r.get("commission", 0)),
                "strategy_name": r.get("strategy_name") or "unknown",
                "signal_reason": r.get("signal_reason") or "",
                "executed_at": r.get("executed_at"),
                "amount": float(r["price"]) * int(r["quantity"]),
            })
    data["trades"] = trades
    data["trades_count"] = len(trades)

    # 6. 净值曲线（从 daily_performance + 实时计算）
    # daily_performance 表里可能数据不全，我们重建一份
    equity_curve = build_equity_curve(db, initial_capital, trades)
    data["equity_curve"] = equity_curve

    # 7. 策略表现（按 strategy_name 聚合）
    strategy_stats = {}
    for t in trades:
        sn = t["strategy_name"]
        if sn not in strategy_stats:
            strategy_stats[sn] = {
                "strategy": sn,
                "buy_count": 0,
                "sell_count": 0,
                "total_amount": 0.0,
                "total_commission": 0.0,
                "first_trade": t["executed_at"],
                "last_trade": t["executed_at"],
            }
        s = strategy_stats[sn]
        if t["side"] == "buy":
            s["buy_count"] += 1
        else:
            s["sell_count"] += 1
        s["total_amount"] += t["amount"]
        s["total_commission"] += t["commission"]
        if t["executed_at"] > s["last_trade"]:
            s["last_trade"] = t["executed_at"]
    data["strategy_stats"] = list(strategy_stats.values())

    # 8. Watchlist 配置
    cfg = ConfigLoader(os.path.join(project_root, "config")).get_strategies_config()
    data["watchlist_total"] = len(cfg.get("watchlist", []))
    data["per_symbol_count"] = len(cfg.get("per_symbol_strategies", {}) or {})

    return data


def build_equity_curve(db: DatabaseManager, initial: float, trades: list) -> list:
    """
    构建每日净值曲线。

    阶段8 Fix Round 3：优先从 daily_performance 表读（真实数据）。
    如果表空或不足，才降级到从 trades 近似估算。
    """
    # 优先从 daily_performance 表读
    try:
        with db._get_conn() as conn:
            cur = conn.execute(
                "SELECT date, total_assets, cumulative_return, trade_count "
                "FROM daily_performance "
                "WHERE trade_mode = 'paper' "
                "ORDER BY date ASC"
            )
            rows = cur.fetchall()
        if rows and len(rows) >= 1:
            points = [
                {
                    "date": r["date"] if isinstance(r, dict) else r[0],
                    "value": float(r["total_assets"] if isinstance(r, dict) else r[1]),
                    "cum_return": float(r["cumulative_return"] if isinstance(r, dict) else r[2]) if r[2] is not None else 0,
                    "trades": int(r["trade_count"] if isinstance(r, dict) else r[3]) if r[3] is not None else 0,
                }
                for r in rows
            ]
            return points
    except Exception as e:
        logger.warning(f"从 daily_performance 读失败，降级到近似重建：{e}")

    # 降级：从 trades 近似（仅兜底）
    if not trades:
        return [{"date": datetime.now().strftime("%Y-%m-%d"), "value": initial}]

    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["executed_at"]).dt.date.astype(str)
    df["cash_flow"] = df.apply(
        lambda r: -(r["amount"] + r["commission"])
        if r["side"] == "buy"
        else r["amount"] - r["commission"],
        axis=1,
    )
    daily_cf = df.groupby("date")["cash_flow"].sum().reset_index()
    points = []
    for _, row in daily_cf.iterrows():
        points.append({"date": row["date"], "value": initial})  # 占位

    # 真实总资产从 trading_state 拿（最新一次）
    # 这里我们简化：起点 = initial，终点 = 实际 total_assets（在外面填充）
    if not points:
        points = [{"date": datetime.now().strftime("%Y-%m-%d"), "value": initial}]
    return points


# ============================================================
# HTML 模板（单文件 + Chart.js CDN）
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚡ AI QUANT TERMINAL · {generated_at}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/apexcharts@3.49.1/dist/apexcharts.min.js"></script>
<style>
  :root {{
    /* === Cyberpunk 调色盘 === */
    --color-up: #ff3b5c;          /* 霓虹涨红 */
    --color-up-glow: rgba(255, 59, 92, 0.45);
    --color-down: #00ff9c;         /* 霓虹跌绿 */
    --color-down-glow: rgba(0, 255, 156, 0.45);
    --color-neutral: #8a93b8;
    --color-cyan: #00f0ff;         /* 主强调色 - 霓虹青 */
    --color-cyan-glow: rgba(0, 240, 255, 0.55);
    --color-purple: #b388ff;       /* 副强调色 - 霓虹紫 */
    --color-purple-glow: rgba(179, 136, 255, 0.45);
    --color-pink: #ff4dd2;
    --color-amber: #ffb300;

    /* === 背景层级 === */
    --color-bg: #050817;            /* 最深底色 */
    --color-bg-2: #0a0e27;
    --color-card-bg: rgba(16, 22, 51, 0.55); /* 毛玻璃卡片 */
    --color-card-border: rgba(0, 240, 255, 0.18);
    --color-card-border-hover: rgba(0, 240, 255, 0.55);
    --color-border: rgba(138, 147, 184, 0.15);

    --color-text: #e6ebff;
    --color-text-light: #8a93b8;
    --color-text-muted: #5a6488;

    --color-warning: #ffb300;
    --color-critical: #ff3b5c;
    --color-info: #00f0ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  /* === 全局背景：深空 + 渐变光晕 + 网格 === */
  html, body {{ background: var(--color-bg); }}
  body {{
    font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, "PingFang SC", Arial, sans-serif;
    color: var(--color-text);
    padding: 24px;
    line-height: 1.6;
    min-height: 100vh;
    background:
      radial-gradient(circle at 15% 10%, rgba(0, 240, 255, 0.08), transparent 45%),
      radial-gradient(circle at 85% 0%, rgba(179, 136, 255, 0.08), transparent 50%),
      radial-gradient(circle at 50% 100%, rgba(255, 77, 210, 0.05), transparent 55%),
      linear-gradient(180deg, #050817 0%, #0a0e27 50%, #050817 100%);
    background-attachment: fixed;
    position: relative;
    overflow-x: hidden;
  }}
  /* 网格底纹 */
  body::before {{
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(rgba(0, 240, 255, 0.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 240, 255, 0.025) 1px, transparent 1px);
    background-size: 48px 48px;
    pointer-events: none; z-index: 0;
  }}

  .container {{ max-width: 1480px; margin: 0 auto; position: relative; z-index: 1; }}

  /* === 头部：霓虹 HUD === */
  header {{
    background: linear-gradient(135deg, rgba(16, 22, 51, 0.85) 0%, rgba(28, 17, 65, 0.85) 100%);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    color: var(--color-text);
    padding: 28px 36px;
    border-radius: 16px;
    margin-bottom: 24px;
    border: 1px solid var(--color-card-border);
    box-shadow:
      0 0 40px rgba(0, 240, 255, 0.12),
      0 8px 32px rgba(0, 0, 0, 0.45),
      inset 0 1px 0 rgba(255, 255, 255, 0.08);
    position: relative;
    overflow: hidden;
  }}
  header::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, transparent, var(--color-cyan), var(--color-purple), transparent);
    animation: scan-line 4s linear infinite;
  }}
  @keyframes scan-line {{
    0% {{ transform: translateX(-100%); }}
    100% {{ transform: translateX(100%); }}
  }}
  header h1 {{
    font-family: 'Orbitron', sans-serif;
    font-size: 30px;
    font-weight: 900;
    letter-spacing: 3px;
    background: linear-gradient(90deg, #00f0ff 0%, #b388ff 50%, #ff4dd2 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: 0 0 30px rgba(0, 240, 255, 0.3);
  }}
  header h1 .live-dot {{
    display: inline-block;
    width: 10px; height: 10px;
    background: #00ff9c;
    border-radius: 50%;
    margin-right: 12px;
    box-shadow: 0 0 12px #00ff9c, 0 0 24px #00ff9c;
    animation: pulse 1.6s ease-in-out infinite;
    vertical-align: middle;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.85); }}
  }}
  header .meta {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--color-text-light);
    margin-top: 10px;
    letter-spacing: 0.5px;
  }}
  header .meta .sep {{ color: var(--color-cyan); margin: 0 10px; opacity: 0.6; }}

  .grid {{ display: grid; gap: 20px; }}
  .grid-2 {{ grid-template-columns: 1fr 1fr; }}
  .grid-4 {{ grid-template-columns: repeat(4, 1fr); }}
  @media (max-width: 900px) {{ .grid-2, .grid-4 {{ grid-template-columns: 1fr; }} }}

  /* === 毛玻璃卡片 === */
  .card {{
    background: var(--color-card-bg);
    backdrop-filter: blur(24px) saturate(140%);
    -webkit-backdrop-filter: blur(24px) saturate(140%);
    padding: 22px 26px;
    border-radius: 14px;
    border: 1px solid var(--color-card-border);
    box-shadow:
      0 8px 32px rgba(0, 0, 0, 0.35),
      inset 0 1px 0 rgba(255, 255, 255, 0.06);
    transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
  }}
  .card::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--color-cyan-glow), transparent);
    opacity: 0; transition: opacity 0.35s;
  }}
  .card:hover {{
    border-color: var(--color-card-border-hover);
    transform: translateY(-3px);
    box-shadow:
      0 12px 48px rgba(0, 0, 0, 0.5),
      0 0 32px rgba(0, 240, 255, 0.15),
      inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }}
  .card:hover::before {{ opacity: 1; }}

  .card h2 {{
    font-family: 'Space Grotesk', sans-serif;
    font-size: 13px;
    font-weight: 700;
    color: var(--color-cyan);
    margin-bottom: 18px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--color-border);
    text-transform: uppercase;
    letter-spacing: 2.2px;
    display: flex; align-items: center; gap: 10px;
    text-shadow: 0 0 12px rgba(0, 240, 255, 0.4);
  }}
  .card h2 .badge {{
    background: linear-gradient(135deg, var(--color-cyan), var(--color-purple));
    color: var(--color-bg);
    font-size: 10px;
    padding: 3px 9px;
    border-radius: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    box-shadow: 0 0 12px var(--color-cyan-glow);
  }}

  /* === KPI 数字卡片 === */
  .kpi {{ text-align: center; padding: 12px 0; position: relative; }}
  .kpi .label {{
    color: var(--color-text-light);
    font-size: 10px;
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 2.5px;
    font-weight: 600;
  }}
  .kpi .value {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 28px;
    font-weight: 700;
    color: var(--color-text);
    text-shadow: 0 0 20px rgba(0, 240, 255, 0.25);
    letter-spacing: 0.5px;
  }}
  .kpi .sub {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--color-text-light);
    margin-top: 6px;
    font-weight: 600;
  }}

  /* === 涨红跌绿（霓虹版）=== */
  .up {{
    color: var(--color-up) !important;
    text-shadow: 0 0 8px var(--color-up-glow);
  }}
  .down {{
    color: var(--color-down) !important;
    text-shadow: 0 0 8px var(--color-down-glow);
  }}
  .neutral {{ color: var(--color-neutral); }}

  /* === 表格 === */
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-family: 'Space Grotesk', sans-serif;
  }}
  table th {{
    text-align: left;
    padding: 12px 10px;
    background: rgba(0, 240, 255, 0.04);
    color: var(--color-cyan);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.8px;
    font-weight: 700;
    border-bottom: 1px solid var(--color-card-border);
  }}
  table td {{
    padding: 12px 10px;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
  }}
  table tr {{ transition: background 0.2s; }}
  table tr:hover td {{ background: rgba(0, 240, 255, 0.04); }}
  table .num {{
    text-align: right;
    font-family: 'JetBrains Mono', monospace;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }}
  table strong {{ color: var(--color-cyan); font-weight: 700; }}

  /* === 图表容器 === */
  .chart-container {{
    position: relative;
    height: 320px;
    margin-top: 8px;
  }}
  .chart-container.tall {{ height: 380px; }}

  /* === 异常 issue 卡片 === */
  .issue {{
    padding: 12px 16px;
    margin-bottom: 10px;
    border-radius: 8px;
    border-left: 3px solid;
    font-size: 13px;
    backdrop-filter: blur(8px);
    transition: all 0.25s;
  }}
  .issue:hover {{ transform: translateX(4px); }}
  .issue.warning {{
    background: rgba(255, 179, 0, 0.08);
    border-color: var(--color-warning);
    color: #ffd54f;
    box-shadow: inset 0 0 20px rgba(255, 179, 0, 0.05);
  }}
  .issue.critical {{
    background: rgba(255, 59, 92, 0.08);
    border-color: var(--color-critical);
    color: #ff8a9d;
    box-shadow: inset 0 0 20px rgba(255, 59, 92, 0.08);
  }}
  .issue.info {{
    background: rgba(0, 240, 255, 0.06);
    border-color: var(--color-info);
    color: #80f5ff;
  }}
  .issue .code {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    margin-right: 10px;
    letter-spacing: 0.5px;
  }}
  .issue.empty {{
    background: rgba(0, 255, 156, 0.06);
    border-color: var(--color-down);
    color: var(--color-down);
    text-align: center;
    text-shadow: 0 0 12px var(--color-down-glow);
    font-weight: 600;
  }}

  /* === 时间线 === */
  .timeline {{
    max-height: 520px;
    overflow-y: auto;
    padding-right: 10px;
  }}
  .timeline::-webkit-scrollbar {{ width: 6px; }}
  .timeline::-webkit-scrollbar-thumb {{
    background: var(--color-cyan);
    border-radius: 3px;
    box-shadow: 0 0 8px var(--color-cyan-glow);
  }}
  .timeline-item {{
    display: flex; gap: 14px;
    padding: 14px 4px;
    border-bottom: 1px solid var(--color-border);
    transition: all 0.25s;
  }}
  .timeline-item:hover {{
    background: rgba(0, 240, 255, 0.03);
    padding-left: 12px;
  }}
  .timeline-item:last-child {{ border-bottom: none; }}
  .timeline-marker {{
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-top: 8px; flex-shrink: 0;
    position: relative;
  }}
  .timeline-marker::before {{
    content: '';
    position: absolute; inset: -3px;
    border-radius: 50%;
    opacity: 0.4;
    animation: marker-pulse 2s ease-in-out infinite;
  }}
  .timeline-marker.buy {{
    background: var(--color-up);
    box-shadow: 0 0 12px var(--color-up-glow);
  }}
  .timeline-marker.buy::before {{ background: var(--color-up); }}
  .timeline-marker.sell {{
    background: var(--color-down);
    box-shadow: 0 0 12px var(--color-down-glow);
  }}
  .timeline-marker.sell::before {{ background: var(--color-down); }}
  @keyframes marker-pulse {{
    0%, 100% {{ transform: scale(1); opacity: 0.4; }}
    50% {{ transform: scale(1.6); opacity: 0; }}
  }}
  .timeline-content {{
    flex: 1;
    font-size: 13px;
    color: var(--color-text);
  }}
  .timeline-content strong {{ color: var(--color-text); font-weight: 700; }}
  .timeline-content .meta {{
    color: var(--color-text-muted);
    font-size: 11px;
    margin-top: 3px;
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: 0.3px;
  }}

  /* === Check / Cross 状态 === */
  .check {{
    color: var(--color-down);
    font-weight: 700;
    text-shadow: 0 0 8px var(--color-down-glow);
  }}
  .cross {{ color: var(--color-text-muted); opacity: 0.5; }}

  /* === Footer === */
  footer {{
    text-align: center;
    color: var(--color-text-muted);
    font-size: 11px;
    padding: 24px;
    margin-top: 32px;
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    border-top: 1px solid var(--color-border);
  }}
  footer .signature {{
    color: var(--color-cyan);
    text-shadow: 0 0 8px var(--color-cyan-glow);
  }}

  /* 滚动条全局 */
  ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
  ::-webkit-scrollbar-track {{ background: rgba(255, 255, 255, 0.02); }}
  ::-webkit-scrollbar-thumb {{
    background: rgba(0, 240, 255, 0.3);
    border-radius: 4px;
  }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--color-cyan); }}

  /* 进入动画 */
  .card {{ animation: fadeInUp 0.5s ease-out backwards; }}
  .card:nth-child(1) {{ animation-delay: 0.05s; }}
  .card:nth-child(2) {{ animation-delay: 0.1s; }}
  .card:nth-child(3) {{ animation-delay: 0.15s; }}
  .card:nth-child(4) {{ animation-delay: 0.2s; }}
  @keyframes fadeInUp {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
</style>
</head>
<body>
<div class="container">

  <header>
    <h1><span class="live-dot"></span>AI QUANT TERMINAL</h1>
    <div class="meta">
      <span>SYSTEM TIME: {generated_at}</span>
      <span class="sep">//</span>
      <span>LAST SCAN: {last_scan_date}</span>
      <span class="sep">//</span>
      <span>WATCHLIST: {watchlist_total} SYMBOLS</span>
      <span class="sep">//</span>
      <span>PER-SYMBOL: {per_symbol_count} MAPPED</span>
    </div>
  </header>

  <!-- ========== 1. KPI 概览（4 卡片）========== -->
  <div class="grid grid-4" style="margin-bottom: 22px;">
    <div class="card kpi">
      <div class="label">TOTAL EQUITY</div>
      <div class="value">${total_assets_fmt}</div>
      <div class="sub {return_class}">{return_pct_fmt}</div>
    </div>
    <div class="card kpi">
      <div class="label">CASH</div>
      <div class="value">${cash_fmt}</div>
      <div class="sub">{cash_pct} ALLOC</div>
    </div>
    <div class="card kpi">
      <div class="label">MARKET VALUE</div>
      <div class="value">${market_value_fmt}</div>
      <div class="sub">{positions_count} POSITIONS</div>
    </div>
    <div class="card kpi">
      <div class="label">TOTAL TRADES</div>
      <div class="value">{trades_count}</div>
      <div class="sub">FEE ${total_commission_fmt}</div>
    </div>
  </div>

  <!-- ========== 2. 异常 + 净值曲线 ========== -->
  <div class="grid grid-2" style="margin-bottom: 22px;">
    <div class="card">
      <h2>⚠ SYSTEM ALERTS <span class="badge">{issues_count}</span></h2>
      {issues_html}
    </div>
    <div class="card">
      <h2>📈 EQUITY CURVE</h2>
      <div id="equity-chart" class="chart-container"></div>
    </div>
  </div>

  <!-- ========== 3. 持仓概览（饼图 + 表格）========== -->
  <div class="grid grid-2" style="margin-bottom: 22px;">
    <div class="card">
      <h2>◎ POSITION ALLOCATION</h2>
      <div id="positions-pie" class="chart-container"></div>
    </div>
    <div class="card">
      <h2>▣ ACTIVE POSITIONS <span class="badge">{positions_count}</span></h2>
      {positions_table}
    </div>
  </div>

  <!-- ========== 4. ATR 止损追踪面板 ========== -->
  <div class="card" style="margin-bottom: 22px;">
    <h2>🛡 ATR STOP-LOSS TRACKER <span class="badge">{sl_count}</span></h2>
    {stop_loss_table}
  </div>

  <!-- ========== 5. 策略表现对比 ========== -->
  <div class="grid grid-2" style="margin-bottom: 22px;">
    <div class="card">
      <h2>▲ STRATEGY PERFORMANCE</h2>
      <div id="strategy-chart" class="chart-container"></div>
    </div>
    <div class="card">
      <h2>◈ STRATEGY STATS</h2>
      {strategy_table}
    </div>
  </div>

  <!-- ========== 6. 交易历史时间线 ========== -->
  <div class="card">
    <h2>⏱ TRADE TIMELINE <span class="badge">{trades_count}</span></h2>
    <div class="timeline">
      {trades_timeline}
    </div>
  </div>

  <footer>
    <span class="signature">⚡ AI QUANT TERMINAL v2.0</span> · BUILT WITH APEXCHARTS · CYBERPUNK MODE · 涨红跌绿
  </footer>
</div>

<script>
// ===== 数据注入 =====
const equityData = {equity_data_json};
const positionsData = {positions_data_json};
const strategyData = {strategy_data_json};

// ===== Cyberpunk 配色 =====
const COLOR_UP = '#ff3b5c';        // 涨红霓虹
const COLOR_DOWN = '#00ff9c';      // 跌绿霓虹
const COLOR_CYAN = '#00f0ff';
const COLOR_PURPLE = '#b388ff';
const COLOR_PINK = '#ff4dd2';
const COLOR_AMBER = '#ffb300';
const TEXT_LIGHT = '#8a93b8';
const TEXT_MUTED = '#5a6488';
const GRID_LINE = 'rgba(138, 147, 184, 0.08)';
const PALETTE = ['#00f0ff', '#b388ff', '#ff4dd2', '#ffb300', '#00ff9c', '#ff3b5c', '#80f5ff', '#c9a6ff', '#ff8a9d', '#ffd54f'];

// ===== ApexCharts 通用配置 =====
const baseChartConfig = {{
  chart: {{
    fontFamily: '"Space Grotesk", "JetBrains Mono", sans-serif',
    foreColor: TEXT_LIGHT,
    background: 'transparent',
    toolbar: {{ show: false }},
    animations: {{
      enabled: true,
      easing: 'easeinout',
      speed: 800,
      animateGradually: {{ enabled: true, delay: 80 }},
      dynamicAnimation: {{ enabled: true, speed: 350 }}
    }},
    dropShadow: {{ enabled: true, top: 4, blur: 12, color: '#00f0ff', opacity: 0.15 }}
  }},
  grid: {{
    borderColor: GRID_LINE,
    strokeDashArray: 4,
    xaxis: {{ lines: {{ show: false }} }},
    yaxis: {{ lines: {{ show: true }} }},
  }},
  tooltip: {{
    theme: 'dark',
    style: {{ fontFamily: '"JetBrains Mono", monospace', fontSize: '12px' }},
  }},
}};

// ===== 1. 净值曲线（区域+渐变）=====
if (equityData && equityData.length > 0) {{
  const equityValues = equityData.map(d => d.value);
  const isUp = equityValues[equityValues.length - 1] >= equityValues[0];
  const lineColor = isUp ? COLOR_UP : COLOR_DOWN;

  new ApexCharts(document.querySelector('#equity-chart'), {{
    ...baseChartConfig,
    chart: {{
      ...baseChartConfig.chart,
      type: 'area',
      height: 320,
      sparkline: {{ enabled: false }},
    }},
    series: [{{ name: 'TOTAL EQUITY', data: equityValues }}],
    xaxis: {{
      categories: equityData.map(d => d.date),
      labels: {{ style: {{ colors: TEXT_MUTED, fontSize: '10px' }} }},
      axisBorder: {{ color: GRID_LINE }},
      axisTicks: {{ color: GRID_LINE }},
    }},
    yaxis: {{
      labels: {{
        style: {{ colors: TEXT_MUTED, fontSize: '10px' }},
        formatter: v => '$' + Math.round(v).toLocaleString(),
      }},
    }},
    stroke: {{ curve: 'smooth', width: 2.5, colors: [lineColor] }},
    fill: {{
      type: 'gradient',
      gradient: {{
        shade: 'dark',
        shadeIntensity: 1,
        opacityFrom: 0.4,
        opacityTo: 0.05,
        stops: [0, 100],
        colorStops: [
          {{ offset: 0, color: lineColor, opacity: 0.4 }},
          {{ offset: 100, color: lineColor, opacity: 0 }},
        ],
      }},
    }},
    markers: {{
      size: 4,
      colors: [lineColor],
      strokeColors: '#0a0e27',
      strokeWidth: 2,
      hover: {{ size: 7 }},
    }},
    dataLabels: {{ enabled: false }},
    tooltip: {{
      ...baseChartConfig.tooltip,
      y: {{ formatter: v => '$' + v.toLocaleString(undefined, {{maximumFractionDigits: 2}}) }},
    }},
  }}).render();
}} else {{
  document.querySelector('#equity-chart').innerHTML = '<p style="color: ' + TEXT_MUTED + '; text-align: center; padding: 80px 0; font-family: monospace;">// NO DATA POINTS</p>';
}}

// ===== 2. 持仓饼图（环形）=====
if (positionsData && positionsData.length > 0) {{
  new ApexCharts(document.querySelector('#positions-pie'), {{
    ...baseChartConfig,
    chart: {{
      ...baseChartConfig.chart,
      type: 'donut',
      height: 320,
    }},
    series: positionsData.map(p => p.market_value),
    labels: positionsData.map(p => p.symbol),
    colors: PALETTE,
    stroke: {{ width: 2, colors: ['#0a0e27'] }},
    legend: {{
      position: 'right',
      labels: {{ colors: TEXT_LIGHT }},
      fontSize: '12px',
      fontFamily: '"JetBrains Mono", monospace',
      markers: {{ width: 10, height: 10, radius: 2 }},
      itemMargin: {{ horizontal: 8, vertical: 4 }},
    }},
    dataLabels: {{
      enabled: true,
      style: {{
        fontSize: '11px',
        fontFamily: '"JetBrains Mono", monospace',
        fontWeight: 700,
        colors: ['#0a0e27'],
      }},
      formatter: (val) => val.toFixed(1) + '%',
      dropShadow: {{ enabled: false }},
    }},
    plotOptions: {{
      pie: {{
        donut: {{
          size: '62%',
          background: 'transparent',
          labels: {{
            show: true,
            name: {{ show: true, fontSize: '11px', color: TEXT_MUTED, offsetY: -5 }},
            value: {{
              show: true,
              fontSize: '20px',
              color: COLOR_CYAN,
              fontFamily: '"JetBrains Mono", monospace',
              fontWeight: 700,
              formatter: v => '$' + Math.round(v).toLocaleString(),
            }},
            total: {{
              show: true,
              label: 'TOTAL MV',
              color: TEXT_MUTED,
              fontSize: '10px',
              fontFamily: '"Space Grotesk", sans-serif',
              formatter: w => '$' + Math.round(w.globals.seriesTotals.reduce((a, b) => a + b, 0)).toLocaleString(),
            }},
          }},
        }},
        expandOnClick: false,
      }},
    }},
    tooltip: {{
      ...baseChartConfig.tooltip,
      y: {{ formatter: v => '$' + v.toLocaleString(undefined, {{maximumFractionDigits: 0}}) }},
    }},
  }}).render();
}} else {{
  document.querySelector('#positions-pie').innerHTML = '<p style="color: ' + TEXT_MUTED + '; text-align: center; padding: 80px 0; font-family: monospace;">// NO POSITIONS</p>';
}}

// ===== 3. 策略柱状图 =====
if (strategyData && strategyData.length > 0) {{
  new ApexCharts(document.querySelector('#strategy-chart'), {{
    ...baseChartConfig,
    chart: {{
      ...baseChartConfig.chart,
      type: 'bar',
      height: 320,
      stacked: false,
    }},
    series: [
      {{ name: 'BUY', data: strategyData.map(s => s.buy_count) }},
      {{ name: 'SELL', data: strategyData.map(s => s.sell_count) }},
    ],
    colors: [COLOR_UP, COLOR_DOWN],
    xaxis: {{
      categories: strategyData.map(s => s.strategy.toUpperCase()),
      labels: {{
        style: {{
          colors: TEXT_LIGHT,
          fontSize: '11px',
          fontFamily: '"JetBrains Mono", monospace',
        }},
      }},
      axisBorder: {{ color: GRID_LINE }},
      axisTicks: {{ color: GRID_LINE }},
    }},
    yaxis: {{
      labels: {{ style: {{ colors: TEXT_MUTED, fontSize: '10px' }} }},
    }},
    plotOptions: {{
      bar: {{
        borderRadius: 6,
        columnWidth: '55%',
        dataLabels: {{ position: 'top' }},
      }},
    }},
    dataLabels: {{
      enabled: true,
      offsetY: -22,
      style: {{
        fontSize: '11px',
        fontFamily: '"JetBrains Mono", monospace',
        colors: [TEXT_LIGHT],
      }},
    }},
    legend: {{
      position: 'top',
      horizontalAlign: 'right',
      labels: {{ colors: TEXT_LIGHT }},
      fontSize: '11px',
      fontFamily: '"Space Grotesk", sans-serif',
      markers: {{ width: 10, height: 10, radius: 2 }},
    }},
    grid: {{ ...baseChartConfig.grid }},
    tooltip: baseChartConfig.tooltip,
  }}).render();
}} else {{
  document.querySelector('#strategy-chart').innerHTML = '<p style="color: ' + TEXT_MUTED + '; text-align: center; padding: 80px 0; font-family: monospace;">// NO STRATEGY DATA</p>';
}}
</script>
</body>
</html>
"""


# ============================================================
# 渲染辅助函数
# ============================================================

def fmt_money(v: float, decimals: int = 2) -> str:
    return f"{v:,.{decimals}f}"

def pnl_class(v: float) -> str:
    if v > 0.0001:
        return "up"
    if v < -0.0001:
        return "down"
    return "neutral"

def render_issues(issues: list) -> str:
    if not issues:
        return '<div class="issue empty">✅ 无异常，所有校验通过</div>'
    html = []
    for i in issues:
        cls = i["level"].lower()
        html.append(
            f'<div class="issue {cls}">'
            f'<span class="code">[{i["level"]}] {i["code"]}</span>'
            f'{i["msg"]}</div>'
        )
    return "\n".join(html)

def render_positions_table(positions: list) -> str:
    if not positions:
        return "<p style='color: #999; text-align: center; padding: 20px;'>无持仓</p>"
    rows = []
    for p in positions:
        cls = pnl_class(p["pnl"])
        rows.append(
            f"<tr>"
            f"<td><strong>{p['symbol']}</strong></td>"
            f"<td class='num'>{p['quantity']:,}</td>"
            f"<td class='num'>${p['avg_cost']:.2f}</td>"
            f"<td class='num'>${p['current_price']:.2f}</td>"
            f"<td class='num {cls}'>${p['pnl']:+,.2f}</td>"
            f"<td class='num {cls}'>{p['pnl_pct']:+.2%}</td>"
            f"<td class='num'>{p['weight']:.1%}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>标的</th><th class='num'>股数</th><th class='num'>成本</th>"
        "<th class='num'>现价</th><th class='num'>浮盈亏</th>"
        "<th class='num'>收益率</th><th class='num'>权重</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

def render_stop_loss_table(sl_list: list) -> str:
    if not sl_list:
        return "<p style='color: #999; text-align: center; padding: 20px;'>无 ATR 追踪</p>"
    rows = []
    for s in sl_list:
        check = '<span class="check">✅</span>'
        cross = '<span class="cross">⬜</span>'
        tp1 = check if s['tp1_triggered'] else cross
        tp2 = check if s['tp2_triggered'] else cross
        tp3 = check if s['tp3_triggered'] else cross
        be = check if s['stop_moved_to_breakeven'] else cross
        stop_str = f"${s['current_stop']:.2f}" if s.get('current_stop') else "-"
        d2s = f"{s['distance_to_stop']:+.2f}%" if s.get('distance_to_stop') is not None else "-"
        d2t = f"{s['distance_to_tp1']:+.2f}%" if s.get('distance_to_tp1') is not None else "-"
        rows.append(
            f"<tr>"
            f"<td><strong>{s['symbol']}</strong></td>"
            f"<td>{s.get('strategy_name','')}</td>"
            f"<td class='num'>${s['avg_cost']:.2f}</td>"
            f"<td class='num'>${s['current_price']:.2f}</td>"
            f"<td class='num down'>{stop_str}</td>"
            f"<td class='num down'>{d2s}</td>"
            f"<td>{tp1}</td><td>{tp2}</td><td>{tp3}</td><td>{be}</td>"
            f"<td class='num'>{s['remaining_size']:,}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>标的</th><th>策略</th><th class='num'>入场</th>"
        "<th class='num'>现价</th><th class='num'>止损价</th>"
        "<th class='num'>距止损</th>"
        "<th>TP1</th><th>TP2</th><th>TP3</th><th>保本</th>"
        "<th class='num'>剩余</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

def render_strategy_table(stats: list) -> str:
    if not stats:
        return "<p style='color: #999; text-align: center; padding: 20px;'>无交易记录</p>"
    rows = []
    for s in stats:
        rows.append(
            f"<tr>"
            f"<td><strong>{s['strategy']}</strong></td>"
            f"<td class='num'>{s['buy_count']}</td>"
            f"<td class='num'>{s['sell_count']}</td>"
            f"<td class='num'>${s['total_amount']:,.0f}</td>"
            f"<td class='num'>${s['total_commission']:.2f}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>策略</th><th class='num'>买入</th><th class='num'>卖出</th>"
        "<th class='num'>总金额</th><th class='num'>手续费</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

def render_trades_timeline(trades: list, limit: int = 50) -> str:
    if not trades:
        return "<p style='color: #999; text-align: center; padding: 20px;'>无交易记录</p>"
    # 倒序，最新的在上面
    trades_sorted = sorted(trades, key=lambda x: x["executed_at"], reverse=True)[:limit]
    items = []
    for t in trades_sorted:
        side_cls = t["side"]
        side_label = "🟢 BUY" if side_cls == "buy" else "🔴 SELL"
        time_str = t["executed_at"][:19].replace("T", " ")
        reason = t["signal_reason"][:60] + ("..." if len(t["signal_reason"]) > 60 else "")
        items.append(
            f'<div class="timeline-item">'
            f'<div class="timeline-marker {side_cls}"></div>'
            f'<div class="timeline-content">'
            f'<strong>{side_label} {t["symbol"]}</strong> '
            f'{t["quantity"]} 股 @ ${t["price"]:.2f} '
            f'<span style="color: #999;">(${t["amount"]:,.0f})</span>'
            f'<div class="meta">{time_str} · {t["strategy_name"]} · {reason}</div>'
            f'</div>'
            f'</div>'
        )
    if len(trades) > limit:
        items.append(
            f'<div style="text-align: center; color: #999; padding: 12px; font-size: 12px;">'
            f'仅显示最新 {limit} 条（共 {len(trades)} 条）</div>'
        )
    return "\n".join(items)


# ============================================================
# 主函数
# ============================================================

def render_html(data: Dict[str, Any]) -> str:
    acct = data["account"]
    total_assets = acct["total_assets"]
    cash = acct["cash"]
    market_value = acct["market_value"]
    return_pct = acct["return_pct"]
    total_commission = sum(t["commission"] for t in data["trades"])

    return HTML_TEMPLATE.format(
        # 头部
        generated_at=data["generated_at"],
        last_scan_date=acct.get("last_scan_date") or "(无)",
        watchlist_total=data["watchlist_total"],
        per_symbol_count=data["per_symbol_count"],

        # KPI
        total_assets_fmt=fmt_money(total_assets),
        return_pct_fmt=f"{return_pct:+.2%}",
        return_class=pnl_class(return_pct),
        cash_fmt=fmt_money(cash),
        cash_pct=f"{cash / max(total_assets, 1):.1%}",
        market_value_fmt=fmt_money(market_value),
        positions_count=len(data["positions"]),
        trades_count=data["trades_count"],
        total_commission_fmt=fmt_money(total_commission),

        # 各模块
        issues_count=len(data["issues"]),
        issues_html=render_issues(data["issues"]),
        positions_table=render_positions_table(data["positions"]),
        sl_count=len(data["stop_loss"]),
        stop_loss_table=render_stop_loss_table(data["stop_loss"]),
        strategy_table=render_strategy_table(data["strategy_stats"]),
        trades_timeline=render_trades_timeline(data["trades"]),

        # JS 数据注入
        equity_data_json=json.dumps(data["equity_curve"]),
        positions_data_json=json.dumps([{"symbol": p["symbol"], "market_value": p["market_value"]} for p in data["positions"]]),
        strategy_data_json=json.dumps(data["strategy_stats"]),
    )


def main():
    parser = argparse.ArgumentParser(description="生成 AI Quant Dashboard HTML")
    parser.add_argument("--output", type=str, default=None, help="输出路径")
    parser.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = args.output or os.path.join(
        project_root, "output", "dashboard.html"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print(f"📊 收集数据...")
    data = collect_dashboard_data(project_root)

    print(f"🎨 渲染 HTML...")
    html = render_html(data)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Dashboard 已生成: {output_path}")
    print(f"   总资产: ${data['account']['total_assets']:,.2f}")
    print(f"   持仓: {len(data['positions'])} 只")
    print(f"   交易: {data['trades_count']} 条")
    print(f"   异常: {len(data['issues'])} 条")

    if args.open:
        webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    main()

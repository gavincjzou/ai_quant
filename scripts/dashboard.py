#!/usr/bin/env python3
"""
Dashboard CLI - 独立查看 Paper Trading 状态

用法：
    python scripts/dashboard.py --all
    python scripts/dashboard.py --positions      # 仅持仓
    python scripts/dashboard.py --risk           # 仅风控
    python scripts/dashboard.py --alerts 20      # 最近 20 条告警
    python scripts/dashboard.py --recon 2026-04-22  # 查看指定日期对账报告
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from src.data.database import DatabaseManager
from src.monitor.dashboard import Dashboard


def show_positions(db: DatabaseManager):
    """从 DB 查最新持仓快照。"""
    print("\n📊 最新持仓快照（从 DB）")
    print("=" * 60)
    # 取最近一次 daily_performance
    try:
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if not row:
                print("⚠️ 暂无快照数据。请先启动 PaperTrader 跑一轮。")
                return
            keys = [c[0] for c in conn.execute(
                "SELECT * FROM daily_performance LIMIT 0"
            ).description]
            rec = dict(zip(keys, row))
    except Exception as e:
        print(f"❌ 读取失败: {e}")
        return

    print(f"日期：{rec.get('date')}")
    print(f"总资产：${rec.get('total_assets', 0):,.2f}")
    print(f"现金：${rec.get('cash', 0):,.2f}")
    print(f"持仓市值：${rec.get('market_value', 0):,.2f}")
    print(f"当日 PnL：${rec.get('daily_pnl', 0):+,.2f}")
    print(f"累计收益率：{rec.get('cumulative_return', 0):+.2%}")
    print(f"最大回撤：{rec.get('max_drawdown', 0):.2%}")
    print(f"当日交易数：{int(rec.get('trade_count', 0))}")


def show_recent_trades(db: DatabaseManager, limit: int = 10):
    """显示最近 N 条交易。"""
    print(f"\n📋 最近 {limit} 条交易")
    print("=" * 90)
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                f"SELECT executed_at, symbol, side, quantity, price, "
                f"commission, strategy_name, signal_reason "
                f"FROM trades ORDER BY executed_at DESC LIMIT {limit}"
            ).fetchall()
        if not rows:
            print("⚠️ 暂无交易记录。")
            return
        header = f"{'时间':<20} {'标的':<10} {'方向':<6} {'数量':>5} {'价格':>10} {'手续费':>8} {'策略':<10} 原因"
        print(header)
        print("-" * 90)
        for r in rows:
            t, sym, side, qty, px, comm, sn, reason = r
            reason = (reason or "")[:30]
            print(f"{str(t)[:19]:<20} {sym:<10} {side.upper():<6} {qty:>5} "
                  f"${px:>9.2f} ${comm or 0:>7.2f} {sn or '':<10} {reason}")
    except Exception as e:
        print(f"❌ 读取交易失败: {e}")


def show_alerts(limit: int = 20):
    """显示 output/alerts.log 最近 N 条告警。"""
    alert_file = os.path.join("output", "alerts.log")
    print(f"\n🔔 最近告警（{alert_file}）")
    print("=" * 60)
    if not os.path.exists(alert_file):
        print("⚠️ alerts.log 不存在，系统未产生过告警。")
        return

    with open(alert_file, encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print("✅ 暂无告警。")
        return

    # 告警之间以空行分隔
    blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
    for b in blocks[-limit:]:
        print(b)
        print("-" * 60)


def show_recon(date: str):
    """查看指定日期的对账报告。"""
    path = os.path.join("output", "reconciliation", f"{date}.md")
    if not os.path.exists(path):
        print(f"⚠️ 对账报告不存在：{path}")
        return
    print(f"\n📄 对账报告 - {date}")
    print("=" * 60)
    with open(path, encoding="utf-8") as f:
        print(f.read())


def show_backtest_results(db: DatabaseManager, limit: int = 20):
    """展示数据库里的回测结果清单。"""
    print(f"\n🧪 最近 {limit} 条回测结果")
    print("=" * 90)
    try:
        df = db.load_backtest_results()
        if df.empty:
            print("⚠️ 暂无回测结果。")
            return
        df = df.head(limit)
        cols = [c for c in [
            "created_at", "strategy_name", "symbol", "total_return",
            "annual_return", "max_drawdown", "sharpe_ratio",
            "win_rate", "trade_count",
        ] if c in df.columns]
        print(df[cols].to_string(index=False))
    except Exception as e:
        print(f"❌ 读取回测结果失败: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="展示所有面板")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--trades", type=int, nargs="?", const=10, default=0,
                        help="最近N条交易（默认10）")
    parser.add_argument("--alerts", type=int, nargs="?", const=20, default=0,
                        help="最近N条告警（默认20）")
    parser.add_argument("--recon", type=str, default=None,
                        help="查看指定日期对账报告 (YYYY-MM-DD)")
    parser.add_argument("--backtest", type=int, nargs="?", const=20, default=0,
                        help="最近N条回测结果")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)

    print(f"🤖 AI-Quant Dashboard @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.all or args.positions:
        show_positions(db)
    if args.all or args.trades:
        show_recent_trades(db, args.trades or 10)
    if args.all or args.alerts:
        show_alerts(args.alerts or 20)
    if args.all or args.backtest:
        show_backtest_results(db, args.backtest or 20)
    if args.recon:
        show_recon(args.recon)

    # 如果啥都没加，默认展示 --all
    if not any([args.all, args.positions, args.trades, args.alerts,
                args.recon, args.backtest]):
        show_positions(db)
        show_recent_trades(db, 10)
        show_alerts(10)


if __name__ == "__main__":
    main()

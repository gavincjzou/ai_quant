#!/usr/bin/env python3
"""
手动平仓脚本（阶段8 A3 使用）

用途：当 Paper Trading 因为早期 bug 买入了"错配策略"的仓位时，手动平仓以腾出
仓位给正确策略使用。平仓按当前市价（最新日线收盘价）执行。

用法：
    # 显示当前持仓，让用户确认
    python scripts/manual_close_positions.py --list

    # 平掉指定标的
    python scripts/manual_close_positions.py --close AAPL.US AMZN.US GOOGL.US

    # Dry-run 不实际成交
    python scripts/manual_close_positions.py --close AAPL.US --dry-run
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from src.data.database import DatabaseManager
from src.strategy.base_strategy import Signal, TradeSignal
from src.trader.paper_trader import PaperTrader
from src.utils.config_loader import ConfigLoader


def main():
    parser = argparse.ArgumentParser(description="手动平仓")
    parser.add_argument("--list", action="store_true", help="仅列出当前持仓")
    parser.add_argument("--close", nargs="+", help="要平仓的标的列表")
    parser.add_argument("--dry-run", action="store_true", help="只算不交易")
    parser.add_argument("--reason", type=str, default="Manual close - per_symbol 策略映射调整",
                        help="平仓原因（写入 trade_records.signal_reason）")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_loader = ConfigLoader(os.path.join(project_root, "config"))
    risk_cfg = config_loader.get_risk_config()
    db_path = os.path.join(project_root, "data_cache", "quant.db")

    db = DatabaseManager(db_path)
    # 阶段8 Fix Round 2：注入 alerter 让手动平仓也能企业微信推送
    from src.monitor.alerts import get_alerter
    alerter = get_alerter() if not args.dry_run else None
    trader = PaperTrader(
        initial_capital=800000,
        risk_config=risk_cfg,
        db=db,
        alerter=alerter,
    )
    stats = trader.load_state()

    print(f"\n📊 当前账户（load_state 已恢复）:")
    print(f"   现金: ${trader.cash:,.2f}")
    print(f"   持仓: {len(trader.positions)} 只")
    print(f"   总资产: ${trader.total_assets:,.2f}")
    print()

    # --- 列出持仓 ---
    print("=" * 80)
    print(f"{'标的':<12} {'股数':>8} {'成本':>10} {'现价':>10} {'浮盈亏':>12} {'收益率':>10}")
    print("=" * 80)
    for sym, pos in trader.positions.items():
        qty = pos["quantity"]
        cost = pos["avg_cost"]
        cur = pos.get("current_price", cost)
        pnl = (cur - cost) * qty
        ret = (cur - cost) / cost * 100 if cost > 0 else 0
        marker = "  ⚠️" if args.close and sym in args.close else ""
        print(f"{sym:<12} {qty:>8} ${cost:>8.2f} ${cur:>8.2f} ${pnl:>+10.2f} {ret:>+8.2f}%{marker}")
    print("=" * 80)

    if args.list or not args.close:
        return

    # --- 执行平仓 ---
    print(f"\n🎯 准备平仓：{args.close}")
    if args.dry_run:
        print("  (dry-run 模式，不实际执行)")

    for sym in args.close:
        pos = trader.positions.get(sym)
        if not pos or pos["quantity"] <= 0:
            print(f"  ⚠️  {sym} 无持仓，跳过")
            continue

        qty = pos["quantity"]
        # 用当前价（最新日线收盘）作为平仓价
        price = pos.get("current_price") or pos.get("avg_cost")
        if not price:
            # 从 DB 拿最新价
            df = db.load_kline(sym, period="1d")
            price = float(df["close"].iloc[-1]) if df is not None and not df.empty else None
        if not price:
            print(f"  ❌ {sym} 无法获取价格，跳过")
            continue

        print(f"\n▶️ SELL {sym}: qty={qty} @ ${price:.2f}")

        if args.dry_run:
            pnl = (price - pos["avg_cost"]) * qty
            print(f"   dry-run: 预期 PnL ${pnl:+.2f}")
            continue

        # 构造 SELL TradeSignal 并执行
        signal = TradeSignal(
            symbol=sym,
            signal=Signal.SELL,
            price=price,
            quantity=qty,
            reason=args.reason,
            confidence=1.0,
            strategy_name="manual_close",
        )
        ok = trader.execute_signal(signal)
        if ok:
            new_pos = trader.positions.get(sym)
            remaining = new_pos["quantity"] if new_pos else 0
            print(f"   ✅ 执行成功，剩余持仓: {remaining}")
        else:
            print(f"   ❌ 执行失败")

    if not args.dry_run:
        print(f"\n📊 平仓后账户:")
        print(f"   现金: ${trader.cash:,.2f}")
        print(f"   持仓: {len(trader.positions)} 只 ({list(trader.positions.keys())})")
        print(f"   总资产: ${trader.total_assets:,.2f}")
        print(f"\n  💾 状态已自动保存到 SQLite（execute_signal 内部调用 save_state）")


if __name__ == "__main__":
    main()

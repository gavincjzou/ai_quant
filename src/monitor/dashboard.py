"""
Dashboard - 持仓与绩效仪表盘
命令行展示当前持仓、浮动盈亏、累计收益等。
"""

from datetime import datetime
from typing import Dict, Optional

from loguru import logger

from src.data.database import DatabaseManager


class Dashboard:
    """命令行仪表盘"""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db

    @staticmethod
    def display_portfolio(summary: dict):
        """
        显示投资组合概览。
        
        Args:
            summary: PaperTrader.get_portfolio_summary() 的返回值
        """
        total = summary.get("total_assets", 0)
        cash = summary.get("cash", 0)
        mv = summary.get("market_value", 0)
        ret = summary.get("return_pct", 0)
        positions = summary.get("positions", {})

        print(f"\n╔{'═'*56}╗")
        print(f"║  {'📊 Portfolio Dashboard':^52}  ║")
        print(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^52}  ║")
        print(f"╠{'═'*56}╣")
        print(f"║  Total Assets:  ${total:>14,.2f}  {'':>22}  ║")
        print(f"║  Cash:          ${cash:>14,.2f}  ({cash/total*100:.1f}%){'':>14}  ║" if total > 0 else f"║  Cash:          ${cash:>14,.2f}{'':>26}  ║")
        print(f"║  Market Value:  ${mv:>14,.2f}  ({mv/total*100:.1f}%){'':>14}  ║" if total > 0 else f"║  Market Value:  ${mv:>14,.2f}{'':>26}  ║")

        # 收益颜色
        ret_str = f"{ret:+.2%}"
        print(f"║  Return:        {ret_str:>15}  {'🟢' if ret >= 0 else '🔴'}{'':>22}  ║")
        print(f"╠{'═'*56}╣")

        if positions:
            print(f"║  {'Symbol':<8} {'Qty':>5} {'Cost':>8} {'Price':>8} {'PnL':>9} {'PnL%':>7} ║")
            print(f"║  {'─'*49}   ║")
            for sym, pos in positions.items():
                pnl = pos.get("pnl", 0)
                pnl_pct = pos.get("pnl_pct", 0)
                icon = "🟢" if pnl >= 0 else "🔴"
                sym_short = sym[:8]
                print(
                    f"║  {sym_short:<8} {pos['qty']:>5} "
                    f"${pos['avg_cost']:>7.2f} "
                    f"${pos['current']:>7.2f} "
                    f"${pnl:>+8.2f} "
                    f"{pnl_pct:>+6.1%} {icon} ║"
                )
        else:
            print(f"║  {'No open positions':^52}  ║")

        print(f"╚{'═'*56}╝\n")

    @staticmethod
    def display_risk_status(risk_statuses: list):
        """显示风控状态"""
        if not risk_statuses:
            return

        print(f"\n┌{'─'*56}┐")
        print(f"│  {'🛡️ Risk Monitor':^52}  │")
        print(f"├{'─'*56}┤")
        print(f"│  {'Symbol':<10} {'PnL%':>8} {'Trail%':>8} {'StopLoss':>10} {'Status':>12} │")
        print(f"│  {'─'*49}   │")

        for s in risk_statuses:
            pnl_pct = s.get("pnl_pct", 0)
            trail = s.get("trail_from_high_pct", 0)
            sl_trigger = s.get("stop_loss_trigger", -0.05)

            # 判断状态
            if pnl_pct <= sl_trigger:
                status = "⚠️ STOP"
            elif trail >= abs(sl_trigger):
                status = "⚠️ TRAIL"
            elif pnl_pct >= s.get("take_profit_trigger", 0.15):
                status = "🎯 TP"
            else:
                status = "✅ OK"

            print(
                f"│  {s['symbol']:<10} "
                f"{pnl_pct:>+7.1%} "
                f"{trail:>+7.1%} "
                f"{sl_trigger:>9.1%} "
                f"{status:>12} │"
            )

        print(f"└{'─'*56}┘\n")

    @staticmethod
    def display_order_summary(order_summary: dict):
        """显示订单汇总"""
        print(f"\n┌{'─'*40}┐")
        print(f"│  {'📋 Order Summary':^36}  │")
        print(f"├{'─'*40}┤")
        print(f"│  Total Orders: {order_summary.get('total_orders', 0):>20}  │")
        for status, count in order_summary.get("by_status", {}).items():
            print(f"│  {status:<15} {count:>20}  │")
        print(f"└{'─'*40}┘\n")

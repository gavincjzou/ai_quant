"""
Reporter - 绩效报告生成器
汇总交易数据，生成日报/周报。
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.data.database import DatabaseManager


class PerformanceReporter:
    """绩效报告生成器"""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def daily_report(
        self,
        trade_mode: str = "paper",
        date: Optional[str] = None,
    ) -> str:
        """
        生成每日绩效报告。
        
        Args:
            trade_mode: paper | live
            date: 日期 YYYY-MM-DD，默认今天
            
        Returns:
            格式化的报告文本
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 获取交易记录
        trades = self.db.load_trades(
            trade_mode=trade_mode,
            start_date=date,
            end_date=date + " 23:59:59",
        )

        lines = [
            f"\n{'='*60}",
            f"  📊 Daily Report - {date} ({trade_mode.upper()})",
            f"{'='*60}",
        ]

        if trades.empty:
            lines.append("  No trades today.")
        else:
            lines.append(f"\n  Today's Trades: {len(trades)}")
            lines.append(
                f"  {'Time':<20} {'Symbol':<10} {'Side':<6} "
                f"{'Qty':>5} {'Price':>10} {'Strategy':<12}"
            )
            lines.append(f"  {'-'*65}")

            for _, t in trades.iterrows():
                time_str = str(t.get("executed_at", ""))[:19]
                lines.append(
                    f"  {time_str:<20} {t['symbol']:<10} "
                    f"{t['side'].upper():<6} {t['quantity']:>5} "
                    f"${t['price']:>9.2f} {t.get('strategy_name', ''):12}"
                )

        lines.append(f"{'='*60}\n")
        report = "\n".join(lines)
        logger.info(report)
        return report

    def weekly_report(
        self,
        trade_mode: str = "paper",
    ) -> str:
        """生成周报"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        trades = self.db.load_trades(
            trade_mode=trade_mode,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d 23:59:59"),
        )

        lines = [
            f"\n{'='*60}",
            f"  📊 Weekly Report ({trade_mode.upper()})",
            f"  {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}",
            f"{'='*60}",
            f"\n  Total Trades: {len(trades)}",
        ]

        if not trades.empty:
            buy_trades = trades[trades["side"] == "buy"]
            sell_trades = trades[trades["side"] == "sell"]
            lines.append(f"  Buy Orders:   {len(buy_trades)}")
            lines.append(f"  Sell Orders:  {len(sell_trades)}")

            # 按策略统计
            if "strategy_name" in trades.columns:
                by_strategy = trades.groupby("strategy_name").size()
                lines.append(f"\n  Trades by Strategy:")
                for strategy, count in by_strategy.items():
                    lines.append(f"    {strategy}: {count}")

            # 按标的统计
            by_symbol = trades.groupby("symbol").size()
            lines.append(f"\n  Trades by Symbol:")
            for symbol, count in by_symbol.items():
                lines.append(f"    {symbol}: {count}")

        lines.append(f"\n{'='*60}\n")
        report = "\n".join(lines)
        logger.info(report)
        return report

    def backtest_comparison_report(self) -> str:
        """生成回测结果对比报告"""
        results = self.db.load_backtest_results()
        if results.empty:
            return "No backtest results found."

        lines = [
            f"\n{'='*80}",
            f"  📊 Backtest Results Comparison",
            f"{'='*80}",
            f"  {'Strategy':<12} {'Symbol':<10} {'Return':>10} {'Annual':>10} "
            f"{'MaxDD':>8} {'Sharpe':>8} {'WinRate':>8} {'Trades':>7}",
            f"  {'-'*75}",
        ]

        for _, r in results.iterrows():
            lines.append(
                f"  {str(r.get('strategy_name', '')):<12} "
                f"{str(r.get('symbol', '')):<10} "
                f"{r.get('total_return', 0):>10.2%} "
                f"{r.get('annual_return', 0):>10.2%} "
                f"{r.get('max_drawdown', 0):>8.2%} "
                f"{r.get('sharpe_ratio', 0):>8.2f} "
                f"{r.get('win_rate', 0):>8.2%} "
                f"{int(r.get('trade_count', 0)):>7d}"
            )

        lines.append(f"{'='*80}\n")
        report = "\n".join(lines)
        return report

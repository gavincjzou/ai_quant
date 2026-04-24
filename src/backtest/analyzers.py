"""
Analyzers - 绩效分析器
计算回测关键绩效指标：年化收益率、最大回撤、夏普比率、胜率等。
"""

from typing import Dict, List, Optional
from datetime import datetime

import pandas as pd
import numpy as np
from loguru import logger


class PerformanceAnalyzer:
    """回测绩效分析器"""

    @staticmethod
    def calculate_metrics(
        trades: List[dict],
        equity_curve: pd.Series,
        initial_capital: float,
        risk_free_rate: float = 0.05,
    ) -> Dict:
        """
        计算全套绩效指标。
        
        Args:
            trades: 交易记录列表，每条 dict 包含:
                    {side, symbol, price, quantity, pnl, entry_date, exit_date}
            equity_curve: 每日净值 Series (index=date, value=portfolio_value)
            initial_capital: 初始资金
            risk_free_rate: 无风险利率（年化）
            
        Returns:
            绩效指标字典
        """
        metrics = {}
        
        if equity_curve.empty:
            return {"error": "Empty equity curve"}

        final_value = equity_curve.iloc[-1]
        
        # 1. 总收益率
        total_return = (final_value - initial_capital) / initial_capital
        metrics["total_return"] = total_return

        # 2. 年化收益率
        days = (equity_curve.index[-1] - equity_curve.index[0]).days
        if days > 0:
            annual_return = (1 + total_return) ** (365.0 / days) - 1
        else:
            annual_return = 0
        metrics["annual_return"] = annual_return

        # 3. 最大回撤
        rolling_max = equity_curve.cummax()
        drawdown = (equity_curve - rolling_max) / rolling_max
        max_drawdown = drawdown.min()
        metrics["max_drawdown"] = abs(max_drawdown)

        # 最大回撤持续天数
        dd_start = None
        dd_end = None
        max_dd_duration = 0
        current_dd_start = None
        for i, val in enumerate(drawdown):
            if val < 0 and current_dd_start is None:
                current_dd_start = drawdown.index[i]
            elif val >= 0 and current_dd_start is not None:
                duration = (drawdown.index[i] - current_dd_start).days
                if duration > max_dd_duration:
                    max_dd_duration = duration
                    dd_start = current_dd_start
                    dd_end = drawdown.index[i]
                current_dd_start = None
        metrics["max_drawdown_duration_days"] = max_dd_duration

        # 4. 夏普比率
        daily_returns = equity_curve.pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            daily_rf = (1 + risk_free_rate) ** (1.0 / 252) - 1
            excess_returns = daily_returns - daily_rf
            sharpe = np.sqrt(252) * excess_returns.mean() / excess_returns.std()
        else:
            sharpe = 0
        metrics["sharpe_ratio"] = sharpe

        # 5. Sortino 比率
        if len(daily_returns) > 1:
            daily_rf = (1 + risk_free_rate) ** (1.0 / 252) - 1
            excess_returns = daily_returns - daily_rf
            downside_returns = excess_returns[excess_returns < 0]
            if len(downside_returns) > 0 and downside_returns.std() > 0:
                sortino = np.sqrt(252) * excess_returns.mean() / downside_returns.std()
            else:
                sortino = float('inf') if excess_returns.mean() > 0 else 0
        else:
            sortino = 0
        metrics["sortino_ratio"] = sortino

        # 5b. Calmar 比率：年化收益 / |最大回撤|
        max_dd = metrics.get("max_drawdown", 0)
        if max_dd and max_dd > 0:
            metrics["calmar_ratio"] = annual_return / max_dd
        else:
            metrics["calmar_ratio"] = None  # MaxDD=0 时不可计算

        # 6. 交易统计
        if trades:
            closed_trades = [t for t in trades if "pnl" in t]
            winning = [t for t in closed_trades if t.get("pnl", 0) > 0]
            losing = [t for t in closed_trades if t.get("pnl", 0) <= 0]

            metrics["trade_count"] = len(closed_trades)
            metrics["win_count"] = len(winning)
            metrics["loss_count"] = len(losing)

            # 胜率
            if closed_trades:
                metrics["win_rate"] = len(winning) / len(closed_trades)
            else:
                metrics["win_rate"] = 0

            # 盈亏比
            avg_win = np.mean([t["pnl"] for t in winning]) if winning else 0
            avg_loss = abs(np.mean([t["pnl"] for t in losing])) if losing else 1
            metrics["profit_loss_ratio"] = avg_win / avg_loss if avg_loss > 0 else float('inf')

            # 平均持仓天数
            holding_days = []
            for t in closed_trades:
                if "entry_date" in t and "exit_date" in t:
                    try:
                        entry = pd.to_datetime(t["entry_date"])
                        exit_ = pd.to_datetime(t["exit_date"])
                        holding_days.append((exit_ - entry).days)
                    except Exception:
                        pass
            metrics["avg_holding_days"] = np.mean(holding_days) if holding_days else 0

            # 最大单笔盈亏
            if closed_trades:
                metrics["max_win"] = max(t.get("pnl", 0) for t in closed_trades)
                metrics["max_loss"] = min(t.get("pnl", 0) for t in closed_trades)
        else:
            metrics["trade_count"] = 0
            metrics["win_rate"] = 0
            metrics["profit_loss_ratio"] = 0
            metrics["avg_holding_days"] = 0

        # 7. 其他
        metrics["initial_capital"] = initial_capital
        metrics["final_value"] = final_value
        metrics["total_days"] = days

        return metrics

    @staticmethod
    def format_report(metrics: dict) -> str:
        """格式化绩效报告为文本"""
        calmar = metrics.get("calmar_ratio")
        calmar_str = f"{calmar:>12.2f}" if calmar is not None else "         N/A"
        lines = [
            "=" * 50,
            "        BACKTEST PERFORMANCE REPORT",
            "=" * 50,
            f"  Initial Capital:     ${metrics.get('initial_capital', 0):>12,.2f}",
            f"  Final Value:         ${metrics.get('final_value', 0):>12,.2f}",
            f"  Total Return:        {metrics.get('total_return', 0):>12.2%}",
            f"  Annual Return:       {metrics.get('annual_return', 0):>12.2%}",
            f"  Max Drawdown:        {metrics.get('max_drawdown', 0):>12.2%}",
            f"  Sharpe Ratio:        {metrics.get('sharpe_ratio', 0):>12.2f}",
            f"  Sortino Ratio:       {metrics.get('sortino_ratio', 0):>12.2f}",
            f"  Calmar Ratio:        {calmar_str}",
            "-" * 50,
            f"  Total Trades:        {metrics.get('trade_count', 0):>12d}",
            f"  Win Rate:            {metrics.get('win_rate', 0):>12.2%}",
            f"  Profit/Loss Ratio:   {metrics.get('profit_loss_ratio', 0):>12.2f}",
            f"  Avg Holding Days:    {metrics.get('avg_holding_days', 0):>12.1f}",
            "=" * 50,
        ]
        return "\n".join(lines)

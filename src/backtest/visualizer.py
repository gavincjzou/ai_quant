"""
Visualizer - 回测可视化
K线图叠加买卖点、资金曲线、回撤曲线。
"""

import os
from typing import Dict, List, Optional

import pandas as pd
import numpy as np
from loguru import logger

try:
    import matplotlib
    matplotlib.use("Agg")  # 非交互后端
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MPL_AVAILABLE = True
except ImportError:
    MPL_AVAILABLE = False

try:
    import mplfinance as mpf
    MPF_AVAILABLE = True
except ImportError:
    MPF_AVAILABLE = False


class BacktestVisualizer:
    """回测结果可视化"""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def plot_equity_curve(
        self,
        equity_curve: pd.Series,
        title: str = "Portfolio Equity Curve",
        save_as: Optional[str] = None,
    ) -> str:
        """
        绘制资金曲线。
        
        Returns:
            图片文件路径
        """
        if not MPL_AVAILABLE:
            logger.warning("matplotlib not available")
            return ""

        fig, axes = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1])
        fig.suptitle(title, fontsize=14, fontweight="bold")

        # 上图：资金曲线
        ax1 = axes[0]
        ax1.plot(equity_curve.index, equity_curve.values, color="#2196F3", linewidth=1.5)
        ax1.fill_between(
            equity_curve.index,
            equity_curve.values,
            equity_curve.values[0],
            alpha=0.1,
            color="#2196F3",
        )
        ax1.set_ylabel("Portfolio Value ($)")
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

        # 下图：回撤
        ax2 = axes[1]
        rolling_max = equity_curve.cummax()
        drawdown = (equity_curve - rolling_max) / rolling_max * 100
        ax2.fill_between(
            drawdown.index, drawdown.values, 0, color="#F44336", alpha=0.4
        )
        ax2.set_ylabel("Drawdown (%)")
        ax2.set_xlabel("Date")
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

        plt.tight_layout()

        filepath = save_as or os.path.join(self.output_dir, "equity_curve.png")
        fig.savefig(filepath, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info(f"Equity curve saved: {filepath}")
        return filepath

    def plot_trades_on_kline(
        self,
        data: pd.DataFrame,
        trades: List[dict],
        symbol: str = "",
        save_as: Optional[str] = None,
    ) -> str:
        """
        在K线图上标注买卖点。
        
        Args:
            data: OHLCV DataFrame (with 'date' column)
            trades: 交易记录列表
            
        Returns:
            图片文件路径
        """
        if not MPF_AVAILABLE:
            logger.warning("mplfinance not available, falling back to matplotlib")
            return self._plot_trades_fallback(data, trades, symbol, save_as)

        df = data.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.index.name = "Date"

        # 构建买卖标记
        buy_markers = []
        sell_markers = []

        for trade in trades:
            entry_date = pd.to_datetime(trade.get("entry_date"))
            exit_date = pd.to_datetime(trade.get("exit_date"))
            if entry_date in df.index:
                buy_markers.append((entry_date, df.loc[entry_date, "low"] * 0.98))
            if exit_date in df.index:
                sell_markers.append((exit_date, df.loc[exit_date, "high"] * 1.02))

        # 创建标记序列
        add_plots = []
        if buy_markers:
            buy_dates, buy_prices = zip(*buy_markers)
            buy_series = pd.Series(np.nan, index=df.index)
            for d, p in zip(buy_dates, buy_prices):
                if d in buy_series.index:
                    buy_series[d] = p
            add_plots.append(
                mpf.make_addplot(buy_series, type="scatter", markersize=80, marker="^", color="red")
            )

        if sell_markers:
            sell_dates, sell_prices = zip(*sell_markers)
            sell_series = pd.Series(np.nan, index=df.index)
            for d, p in zip(sell_dates, sell_prices):
                if d in sell_series.index:
                    sell_series[d] = p
            add_plots.append(
                mpf.make_addplot(sell_series, type="scatter", markersize=80, marker="v", color="green")
            )

        filepath = save_as or os.path.join(self.output_dir, f"{symbol}_kline.png")

        mpf.plot(
            df,
            type="candle",
            style="yahoo",
            title=f"{symbol} - Backtest Trades",
            volume=True,
            addplot=add_plots if add_plots else None,
            savefig=filepath,
            figsize=(16, 8),
        )

        logger.info(f"K-line chart saved: {filepath}")
        return filepath

    def _plot_trades_fallback(
        self,
        data: pd.DataFrame,
        trades: List[dict],
        symbol: str,
        save_as: Optional[str],
    ) -> str:
        """无 mplfinance 时的 fallback 可视化"""
        if not MPL_AVAILABLE:
            return ""

        fig, ax = plt.subplots(figsize=(14, 6))
        dates = pd.to_datetime(data["date"])

        ax.plot(dates, data["close"], color="#333", linewidth=1, label="Close")

        for trade in trades:
            entry_date = pd.to_datetime(trade.get("entry_date"))
            exit_date = pd.to_datetime(trade.get("exit_date"))
            ax.scatter(entry_date, trade.get("entry_price", 0), color="red", marker="^", s=100, zorder=5)
            ax.scatter(exit_date, trade.get("exit_price", 0), color="green", marker="v", s=100, zorder=5)

        ax.set_title(f"{symbol} - Backtest Trades")
        ax.set_xlabel("Date")
        ax.set_ylabel("Price ($)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        filepath = save_as or os.path.join(self.output_dir, f"{symbol}_trades.png")
        fig.savefig(filepath, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return filepath

    def plot_metrics_comparison(
        self,
        results: Dict[str, dict],
        save_as: Optional[str] = None,
    ) -> str:
        """
        绘制多策略/多标的绩效对比图。
        
        Args:
            results: {name: metrics_dict}
        """
        if not MPL_AVAILABLE:
            return ""

        names = list(results.keys())
        metrics_keys = ["total_return", "annual_return", "max_drawdown", "sharpe_ratio", "win_rate"]
        labels = ["Total Return", "Annual Return", "Max Drawdown", "Sharpe", "Win Rate"]

        fig, axes = plt.subplots(1, len(metrics_keys), figsize=(18, 5))
        fig.suptitle("Strategy Performance Comparison", fontsize=14, fontweight="bold")

        for i, (key, label) in enumerate(zip(metrics_keys, labels)):
            ax = axes[i]
            values = [results[n].get(key, 0) for n in names]

            colors = []
            for v in values:
                if key == "max_drawdown":
                    colors.append("#F44336" if v > 0.1 else "#4CAF50")
                else:
                    colors.append("#4CAF50" if v > 0 else "#F44336")

            ax.bar(names, values, color=colors, alpha=0.8)
            ax.set_title(label, fontsize=10)
            ax.tick_params(axis="x", rotation=45)

            if key in ["total_return", "annual_return", "max_drawdown", "win_rate"]:
                ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.1%}"))

        plt.tight_layout()

        filepath = save_as or os.path.join(self.output_dir, "comparison.png")
        fig.savefig(filepath, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info(f"Comparison chart saved: {filepath}")
        return filepath

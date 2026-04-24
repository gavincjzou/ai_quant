"""
Backtest Engine - 回测引擎
基于 backtrader 封装。阶段2 升级：
- 引入 PositionSizer + StopLossManager
- 预计算全序列 ATR（utils.indicators.calc_atr）
- next() 每 bar 先走止损止盈检查，支持部分平仓
- _calc_size 走 PositionSizer，传入当前 ATR
- BacktestEngine.run() 开头调 stop_loss.reset()
- 附带修复：per_share + min_commission 的精确手续费
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Type

import pandas as pd
import numpy as np
from loguru import logger

try:
    import backtrader as bt

    BACKTRADER_AVAILABLE = True
except ImportError:
    BACKTRADER_AVAILABLE = False

from src.backtest.data_feed import create_data_feed
from src.backtest.analyzers import PerformanceAnalyzer
from src.strategy.base_strategy import BaseStrategy, Signal, TradeSignal
from src.risk.position_sizer import PositionSizer
from src.risk.stop_loss import StopLossManager
from src.utils.indicators import calc_atr


# ==============================================================
# 精确手续费：per_share + min_commission（backtrader CommInfo）
# ==============================================================
if BACKTRADER_AVAILABLE:

    class PerShareCommission(bt.CommInfoBase):
        """长桥美股真实手续费模型：每股费用 + 最低佣金。"""

        params = (
            ("per_share", 0.0049 + 0.005),  # 佣金 + 平台费
            ("min_commission", 0.99),
            ("commtype", bt.CommInfoBase.COMM_FIXED),
            ("stocklike", True),
        )

        def _getcommission(self, size, price, pseudoexec):
            shares = abs(size)
            comm = shares * self.p.per_share
            return max(comm, self.p.min_commission)

else:
    PerShareCommission = None  # type: ignore


# ==============================================================
# BT Strategy Adapter
# ==============================================================
class BTStrategyAdapter(bt.Strategy if BACKTRADER_AVAILABLE else object):
    """backtrader Strategy 适配器。

    把我们的 BaseStrategy 接口适配为 backtrader 的 Strategy，
    同时接入 PositionSizer + StopLossManager 做风控。
    """

    params = (
        ("strategy_instance", None),           # BaseStrategy 实例
        ("position_sizer", None),              # PositionSizer 实例
        ("stop_loss_manager", None),           # StopLossManager 实例
        ("atr_series", None),                  # 预计算的 ATR pd.Series（index 对齐 data["date"]）
        ("position_mode", "fixed_pct"),        # fixed_pct | risk_based_atr
        ("sl_mode", "legacy"),                 # legacy | atr_442
        ("lookback", 60),  # 阶段6升级：提到 60 以支持 RSI 策略的 MA50 趋势过滤
    )

    def __init__(self):
        self.strategy: BaseStrategy = self.params.strategy_instance
        self._my_sizer: Optional[PositionSizer] = self.params.position_sizer
        self._my_sl_mgr: Optional[StopLossManager] = self.params.stop_loss_manager
        self.atr_series: Optional[pd.Series] = self.params.atr_series
        self.position_mode: str = self.params.position_mode
        self.sl_mode: str = self.params.sl_mode

        self.trade_log: List[dict] = []
        self._pending_entry: dict = {}
        self.equity_log: List[dict] = []
        self._symbol = None  # cache data._name

        # ATR 索引游标：每根 bar 推进一个
        self._bar_idx = -1

    # ----------------------------------------------------------
    # 每根 bar 执行
    # ----------------------------------------------------------
    def next(self):
        self._bar_idx += 1
        current_dt = bt.num2date(self.data.datetime[0])
        current_price = float(self.data.close[0])
        symbol = self._symbol or self.data._name or "UNKNOWN"
        self._symbol = symbol

        # 1) 记录净值
        self.equity_log.append({
            "date": current_dt,
            "value": self.broker.getvalue(),
        })

        # 2) 先走止损止盈检查（有持仓时）
        if self._my_sl_mgr is not None and self.position:
            self._my_sl_mgr.update_price(symbol, current_price)
            signals = self._my_sl_mgr.check_all()
            for sig in signals:
                qty = min(sig.quantity, self.position.size)
                if qty <= 0:
                    continue
                self.sell(size=qty)
                # 在 sl_mgr 中减少 remaining
                self._my_sl_mgr.reduce_position(symbol, qty)
                # 记录部分平仓原因，便于 notify_trade 取用
                self._pending_entry.setdefault(symbol, {})["last_exit_reason"] = sig.reason
                self._pending_entry[symbol]["last_exit_tag"] = sig.strategy_name

                # 如果整体仓位清零，移除 tracker
                if self._my_sl_mgr.get_position(symbol) is None:
                    pass
                else:
                    tracker = self._my_sl_mgr.get_position(symbol)
                    if tracker and tracker.remaining_size <= 0:
                        self._my_sl_mgr.remove_position(symbol)

            # 风控触发了平仓（整或部分）就不再执行下面策略信号
            if signals:
                return

        # 3) 构建 DataFrame 传给策略
        # 性能优化：只取 lookback 长度的窗口，而不是全量历史
        bar_count = len(self.data)
        lookback = min(self.params.lookback, bar_count)
        start = -lookback + 1  # 负索引
        try:
            dates = [bt.num2date(self.data.datetime[i]) for i in range(start, 1)]
            opens = [self.data.open[i] for i in range(start, 1)]
            highs = [self.data.high[i] for i in range(start, 1)]
            lows = [self.data.low[i] for i in range(start, 1)]
            closes = [self.data.close[i] for i in range(start, 1)]
            vols = [self.data.volume[i] for i in range(start, 1)]
        except IndexError:
            return

        df = pd.DataFrame({
            "date": dates, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": vols,
        })

        # 4) 生成信号
        signal = self.strategy.generate_signal(symbol, df)
        if signal is None:
            return

        # 5) 执行信号
        if signal.signal == Signal.BUY and not self.position:
            size = self._calc_size(signal.price, symbol)
            if size > 0:
                self.buy(size=size)
                # 入场时的 ATR（用于 442 止损止盈计算）
                atr_value = self._current_atr()
                self._pending_entry[symbol] = {
                    "entry_date": current_dt,
                    "entry_price": signal.price,
                    "reason": signal.reason,
                    "atr_at_entry": atr_value,
                    "size": size,
                }

        elif signal.signal == Signal.SELL and self.position:
            # 策略主动卖出 -> 全平
            self.sell(size=self.position.size)
            self._pending_entry.setdefault(symbol, {})["last_exit_reason"] = signal.reason
            self._pending_entry[symbol]["last_exit_tag"] = "strategy_sell"
            if self._my_sl_mgr is not None:
                self._my_sl_mgr.remove_position(symbol)

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------
    def _current_atr(self) -> Optional[float]:
        """取当前 bar 对应的 ATR 值（从预计算序列）。"""
        if self.atr_series is None or len(self.atr_series) == 0:
            return None
        idx = self._bar_idx
        if idx < 0 or idx >= len(self.atr_series):
            return None
        v = float(self.atr_series.iloc[idx])
        if np.isnan(v) or v <= 0:
            return None
        return v

    def _calc_size(self, price: float, symbol: str) -> int:
        """用 PositionSizer 计算下单股数。"""
        if self._my_sizer is None:
            # 兜底：legacy 95% 现金
            avail = self.broker.getcash() * 0.95
            return max(int(avail / price), 0) if price > 0 else 0

        total_assets = self.broker.getvalue()
        available_cash = self.broker.getcash()
        return self._my_sizer.calculate(
            price=price,
            total_assets=total_assets,
            available_cash=available_cash,
            existing_position_value=self.position.size * price if self.position else 0,
            mode=self.position_mode,
            atr=self._current_atr(),
            strategy_name=self.strategy.name,
        )

    # ----------------------------------------------------------
    # backtrader 回调
    # ----------------------------------------------------------
    def notify_order(self, order):
        """订单状态回调：在成交后 track_position（用于 442 止损止盈追踪）。"""
        if order.status != order.Completed:
            return
        symbol = self._symbol or self.data._name or "UNKNOWN"
        if order.isbuy() and self._my_sl_mgr is not None:
            entry_info = self._pending_entry.get(symbol, {})
            self._my_sl_mgr.track_position(
                symbol=symbol,
                entry_price=float(order.executed.price),
                size=int(order.executed.size),
                atr=entry_info.get("atr_at_entry"),
                strategy_name=self.strategy.name if self.strategy else None,
            )

    def notify_trade(self, trade):
        """交易完成通知。trade.isclosed 表示本次开平仓合约完整 round-trip。"""
        if trade.isclosed:
            symbol = self._symbol or self.data._name or "UNKNOWN"
            entry_info = self._pending_entry.pop(symbol, {})
            self.trade_log.append({
                "symbol": symbol,
                "pnl": trade.pnl,
                "pnlcomm": trade.pnlcomm,
                "entry_date": str(entry_info.get("entry_date", "")),
                "exit_date": str(bt.num2date(self.data.datetime[0])),
                "entry_price": entry_info.get("entry_price", 0),
                "exit_price": trade.price,
                "size": trade.size,
                "reason": entry_info.get("reason", ""),
                "exit_reason": entry_info.get("last_exit_reason", ""),
                "exit_tag": entry_info.get("last_exit_tag", ""),
            })


# ==============================================================
# Backtest Engine
# ==============================================================
class BacktestEngine:
    """回测引擎。

    Args:
        risk_config: risk.yaml 完整 dict（含 backtest / position / stop_loss / per_strategy_overrides）
    """

    def __init__(self, risk_config: Optional[dict] = None):
        if not BACKTRADER_AVAILABLE:
            raise RuntimeError("backtrader not installed. Run: pip install backtrader")

        self._config = risk_config or {}
        self._bt_config = self._config.get("backtest", self._config)

        # 单例风控组件（跨次 run 共享，但每次 run 必须 reset）
        self._my_sizer = PositionSizer(self._config)
        self._sl_mgr = StopLossManager(self._config)

    # ----------------------------------------------------------
    def run(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        symbol: str = "UNKNOWN",
        initial_capital: Optional[float] = None,
    ) -> Dict:
        """执行单标的回测。"""
        # 每次 run 都要 reset，避免状态串台
        self._sl_mgr.reset()
        # 阶段8 加固：同 strategy 实例跨 run 时也要 reset（如 screen_watchlist 场景）
        try:
            strategy.reset()
        except Exception as e:
            logger.warning(f"strategy.reset() failed (ignored): {e}")

        cerebro = bt.Cerebro()

        # --- 初始资金 ---
        capital = initial_capital or self._bt_config.get("initial_capital", 10000)
        cerebro.broker.setcash(capital)

        # --- 手续费 ---
        self._setup_commission(cerebro)

        # --- 滑点 ---
        slip_config = self._bt_config.get("slippage", {})
        if slip_config.get("type") == "percentage":
            cerebro.broker.set_slippage_perc(slip_config.get("value", 0.001))
        elif slip_config.get("type") == "fixed":
            cerebro.broker.set_slippage_fixed(slip_config.get("value", 0.01))

        # --- 数据 ---
        feed = create_data_feed(data, name=symbol)
        cerebro.adddata(feed)

        # --- 预计算 ATR（索引对齐 data 的行号） ---
        sl_cfg = self._config.get("stop_loss", {}) or {}
        sl_mode = sl_cfg.get("mode", "legacy")
        atr_period = (
            self._config.get("per_strategy_overrides", {})
            .get(strategy.name, {})
            .get("atr_period")
        ) or sl_cfg.get("atr_442", {}).get("atr_period", 14)
        atr_series = calc_atr(data, period=atr_period) if sl_mode == "atr_442" else None
        # ATR 即使 sl_mode=legacy 也可能被 PositionSizer risk_based_atr 用到
        pos_mode = self._config.get("position", {}).get("mode", "fixed_pct")
        if atr_series is None and pos_mode == "risk_based_atr":
            atr_series = calc_atr(data, period=atr_period)

        # --- 策略适配器 ---
        cerebro.addstrategy(
            BTStrategyAdapter,
            strategy_instance=strategy,
            position_sizer=self._my_sizer,
            stop_loss_manager=self._sl_mgr,
            atr_series=atr_series,
            position_mode=pos_mode,
            sl_mode=sl_mode,
        )

        # --- 分析器（保留原 backtrader 内置） ---
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.05)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

        # --- 运行 ---
        logger.info(
            f"Running backtest: {strategy.name} on {symbol}, "
            f"capital=${capital:,.0f}, bars={len(data)}, "
            f"pos_mode={pos_mode}, sl_mode={sl_mode}"
        )
        results = cerebro.run()
        bt_strategy = results[0]

        # --- 提取结果 ---
        if bt_strategy.equity_log:
            eq_df = pd.DataFrame(bt_strategy.equity_log)
            eq_df["date"] = pd.to_datetime(eq_df["date"])
            equity_curve = pd.Series(eq_df["value"].values, index=eq_df["date"])
        else:
            equity_curve = self._build_equity_curve(cerebro, data)

        analyzer = PerformanceAnalyzer()
        metrics = analyzer.calculate_metrics(
            trades=bt_strategy.trade_log,
            equity_curve=equity_curve,
            initial_capital=capital,
        )
        metrics["strategy_name"] = strategy.name
        metrics["symbol"] = symbol
        metrics["start_date"] = str(data["date"].iloc[0])
        metrics["end_date"] = str(data["date"].iloc[-1])
        metrics["params"] = strategy.get_params()
        metrics["pos_mode"] = pos_mode
        metrics["sl_mode"] = sl_mode

        report = analyzer.format_report(metrics)
        logger.info(f"\n{report}")

        return {
            "metrics": metrics,
            "trades": bt_strategy.trade_log,
            "equity_curve": equity_curve,
            "cerebro": cerebro,
        }

    # ----------------------------------------------------------
    def _setup_commission(self, cerebro: "bt.Cerebro") -> None:
        """设置手续费。优先使用 per_share 精确模型。"""
        comm_config = self._bt_config.get("commission", {})
        ctype = comm_config.get("type")

        if ctype == "per_share" and PerShareCommission is not None:
            rate = comm_config.get("rate", 0.0049)
            platform = comm_config.get("platform_fee", 0.005)
            min_comm = comm_config.get("min_commission", 0.99)
            comm_info = PerShareCommission(
                per_share=rate + platform,
                min_commission=min_comm,
            )
            cerebro.broker.addcommissioninfo(comm_info)
        elif ctype == "percentage":
            cerebro.broker.setcommission(commission=comm_config.get("rate", 0.001))
        else:
            cerebro.broker.setcommission(commission=0.001)

    # ----------------------------------------------------------
    def _build_equity_curve(self, cerebro, data: pd.DataFrame) -> pd.Series:
        """从 backtrader 结果构建净值曲线（fallback）。"""
        try:
            strat = cerebro.runstrats[0][0]
            if hasattr(strat, "observers") and strat.observers:
                broker_obs = None
                for obs in strat.observers:
                    if hasattr(obs, "value"):
                        broker_obs = obs
                        break
                if broker_obs is not None:
                    values = list(broker_obs.value)
                    dates = pd.to_datetime(data["date"])
                    min_len = min(len(values), len(dates))
                    values = values[:min_len]
                    dates = dates[:min_len]
                    return pd.Series(values, index=dates)
        except Exception as e:
            logger.debug(f"Could not extract equity from observers: {e}")

        dates = pd.to_datetime(data["date"])
        return pd.Series(
            [cerebro.broker.getvalue()] * len(dates), index=dates
        )

    # ----------------------------------------------------------
    def run_multi(
        self,
        strategy: BaseStrategy,
        data_map: Dict[str, pd.DataFrame],
        initial_capital: Optional[float] = None,
    ) -> Dict[str, Dict]:
        """对多个标的分别回测同一策略。"""
        results = {}
        for symbol, data in data_map.items():
            strategy.reset()
            try:
                result = self.run(strategy, data, symbol, initial_capital)
                results[symbol] = result
            except Exception as e:
                logger.error(f"Backtest failed for {symbol}: {e}")
                results[symbol] = {"error": str(e)}
        return results

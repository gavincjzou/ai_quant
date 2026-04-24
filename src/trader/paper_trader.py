"""
Paper Trader - 模拟交易器
使用真实行情数据模拟交易执行，不实际下单。

阶段8 Fix（跨进程状态持久化）：
- 每次 execute_signal 后自动调 save_state 写 SQLite
- __init__ 之后由 Orchestrator 显式调 load_state 恢复
- 状态范围：cash + positions + StopLossManager + RiskManager + OrderManager counter
"""

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.data.database import DatabaseManager
from src.data.trading_state import TradingState
from src.risk.risk_manager import RiskManager
from src.risk.stop_loss import StopLossManager
from src.strategy.base_strategy import TradeSignal, Signal
from src.trader.order_manager import OrderManager, OrderStatus


# trading_state KV 里的 key 常量
KEY_PAPER_ACCOUNT = "paper.account"       # {cash, initial_capital, last_saved_at}
KEY_PAPER_POSITIONS = "paper.positions"   # {symbol: {quantity, avg_cost, ...}}
KEY_PAPER_STOPLOSS = "paper.stop_loss"    # StopLossManager.dump_state()
KEY_PAPER_RISK = "paper.risk_state"       # RiskManager.dump_state()


class PaperTrader:
    """
    模拟交易器。
    
    使用真实行情数据但不实际下单。
    输出与实盘完全一致的交易记录和绩效。
    """

    def __init__(
        self,
        initial_capital: float = 10000,
        risk_config: Optional[dict] = None,
        db: Optional[DatabaseManager] = None,
        alerter=None,
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, dict] = {}  # {symbol: {quantity, avg_cost, market_value}}
        
        risk_config = risk_config or {}
        self.risk_manager = RiskManager(risk_config)
        # 阶段2起：StopLossManager 接受完整 risk_config（含 per_strategy_overrides）
        self.stop_loss_manager = StopLossManager(risk_config)
        self.order_manager = OrderManager()
        self.db = db

        # 阶段8 Fix Round 2：下沉告警到 PaperTrader（所有调用路径都能告警）
        # 允许外部注入，None 时 execute_signal 不发告警（测试/回测场景）
        self.alerter = alerter

        # 阶段8 Fix：接 TradingState 做跨进程持久化
        self._state_store: Optional[TradingState] = None
        if db is not None:
            try:
                self._state_store = TradingState(db.db_path)
            except Exception as e:
                logger.warning(f"[PaperTrader] TradingState 初始化失败（忽略）：{e}")

        # 手续费配置
        bt_config = risk_config.get("backtest", {}).get("commission", {})
        self._comm_per_share = bt_config.get("rate", 0.0049) + bt_config.get("platform_fee", 0.005)
        self._min_commission = bt_config.get("min_commission", 0.99)
        self._slippage_pct = risk_config.get("backtest", {}).get("slippage", {}).get("value", 0.001)

        # 缓存最近的 ATR（供 track_position 使用）
        self._recent_atr: Dict[str, float] = {}

        # 交易记录
        self.trade_history: List[dict] = []
        self.daily_snapshots: List[dict] = []

        logger.info(f"PaperTrader initialized: capital=${initial_capital:,.2f}")

    @property
    def total_assets(self) -> float:
        """总资产 = 现金 + 持仓市值"""
        market_value = sum(p.get("market_value", 0) for p in self.positions.values())
        return self.cash + market_value

    @property
    def market_value(self) -> float:
        return sum(p.get("market_value", 0) for p in self.positions.values())

    def execute_signal(self, signal: TradeSignal) -> bool:
        """
        执行交易信号。
        
        Args:
            signal: 交易信号
            
        Returns:
            是否执行成功
        """
        # 风控校验
        account_info = {
            "total_assets": self.total_assets,
            "available_cash": self.cash,
        }
        risk_result = self.risk_manager.check_order(
            signal, account_info, self.positions
        )

        if not risk_result.passed:
            logger.warning(f"Order rejected by risk: {risk_result}")
            return False

        quantity = risk_result.approved_quantity
        if quantity <= 0:
            return False

        # 模拟滑点
        if signal.signal == Signal.BUY:
            exec_price = signal.price * (1 + self._slippage_pct)
        else:
            exec_price = signal.price * (1 - self._slippage_pct)

        # 计算手续费
        commission = max(quantity * self._comm_per_share, self._min_commission)

        # 创建并执行订单
        order = self.order_manager.create_order(
            symbol=signal.symbol,
            side=signal.signal.value.lower(),
            quantity=quantity,
            order_type="market",
            price=exec_price,
            strategy_name=signal.strategy_name,
            signal_reason=signal.reason,
            trade_mode="paper",
        )

        # 执行
        if signal.signal == Signal.BUY:
            total_cost = exec_price * quantity + commission
            if total_cost > self.cash:
                # 现金不足，调整数量
                quantity = int((self.cash - commission) / exec_price)
                if quantity <= 0:
                    self.order_manager.update_status(order.order_id, OrderStatus.REJECTED)
                    return False
                total_cost = exec_price * quantity + commission

            self.cash -= total_cost
            self._update_position_buy(signal.symbol, quantity, exec_price)
            # 传入最近的 ATR（阶段4 P0 补丁：atr_442 模式必需）
            atr = self._recent_atr.get(signal.symbol)
            self.stop_loss_manager.track_position(
                symbol=signal.symbol,
                entry_price=self.positions[signal.symbol]["avg_cost"],
                size=self.positions[signal.symbol]["quantity"],
                atr=atr,
                strategy_name=signal.strategy_name,
            )

        elif signal.signal == Signal.SELL:
            pos = self.positions.get(signal.symbol)
            if not pos or pos["quantity"] <= 0:
                self.order_manager.update_status(order.order_id, OrderStatus.REJECTED)
                return False

            sell_qty = min(quantity, pos["quantity"])
            proceeds = exec_price * sell_qty - commission
            self.cash += proceeds
            pnl = (exec_price - pos["avg_cost"]) * sell_qty - commission
            self._update_position_sell(signal.symbol, sell_qty)
            self.stop_loss_manager.remove_position(signal.symbol)

        # 更新订单状态
        self.order_manager.update_status(
            order.order_id, OrderStatus.FILLED,
            filled_quantity=quantity, filled_price=exec_price,
            commission=commission,
        )

        # 记录交易
        trade_record = {
            "order_id": order.order_id,
            "trade_mode": "paper",
            "symbol": signal.symbol,
            "side": signal.signal.value.lower(),
            "quantity": quantity,
            "price": exec_price,
            "commission": commission,
            "strategy_name": signal.strategy_name,
            "signal_reason": signal.reason,
            "executed_at": datetime.now().isoformat(),
        }
        self.trade_history.append(trade_record)

        if self.db:
            self.db.save_trade(trade_record)

        # 更新风控
        self.risk_manager.update_daily_pnl(
            pnl if signal.signal == Signal.SELL else 0
        )
        self.risk_manager.update_portfolio_value(self.total_assets)

        logger.info(
            f"Paper trade executed: {signal.signal.value} {quantity} {signal.symbol} "
            f"@ ${exec_price:.2f}, commission=${commission:.2f}"
        )

        # 阶段8 Fix：每次成交后立即持久化状态
        self.save_state()

        # 阶段8 Fix Round 2：告警下沉到 PaperTrader
        # 所有调用路径（scan/monitor/manual_close_positions/未来任何脚本）都能自动告警
        self._fire_trade_alert(signal, quantity, exec_price, commission)

        return True

    def _fire_trade_alert(
        self,
        signal: "TradeSignal",
        quantity: int,
        exec_price: float,
        commission: float,
    ) -> None:
        """
        成交后告警。
        - BUY：INFO 级别
        - SELL（策略退出/手动平仓）：INFO 级别
        - SELL（风控止损/TP）：WARNING 级别（strategy_name 以 risk_ 开头）
        """
        if self.alerter is None:
            return
        try:
            # 导入放在这里避免循环依赖
            from src.monitor.alerts import AlertLevel

            side_emoji = "🟢" if signal.signal == Signal.BUY else "🔴"
            strat_name = signal.strategy_name or "unknown"
            is_risk_exit = strat_name.startswith("risk_") or strat_name == "manual_close"

            # 判断级别
            if signal.signal == Signal.SELL and strat_name.startswith("risk_"):
                level = AlertLevel.WARNING
                title_prefix = "🛡 风控触发"
            elif strat_name == "manual_close":
                level = AlertLevel.WARNING
                title_prefix = "✋ 手动"
            else:
                level = AlertLevel.INFO
                title_prefix = ""

            title = f"{title_prefix} {signal.signal.value} 成交".strip()

            # 计算总金额 + 浮盈亏（SELL 时）
            amount = quantity * exec_price
            extra = ""
            if signal.signal == Signal.SELL:
                pos = self.positions.get(signal.symbol)
                # 注意：此时持仓已经更新（SELL 后 quantity 可能已减）
                # 为了准确，我们用 commission / quantity 间接推，或用 reason 里已带的信息
                extra = f"\n金额：${amount:,.2f}"
            else:
                extra = f"\n金额：${amount:,.2f}"

            body = (
                f"{side_emoji} {signal.signal.value} {signal.symbol} "
                f"{quantity} 股 @ ${exec_price:.2f}"
                f"{extra}\n"
                f"手续费：${commission:.2f}\n"
                f"策略：{strat_name}\n"
                f"原因：{signal.reason}"
            )

            self.alerter.send(
                body,
                level=level,
                title=title,
                tags=["trade", signal.signal.value.lower(), strat_name],
            )
        except Exception as e:
            # 告警失败不能影响交易主流程
            logger.warning(f"[PaperTrader] _fire_trade_alert 失败（忽略）：{e}")

    def update_prices(self, prices: Dict[str, float]):
        """更新所有持仓的最新价格"""
        for symbol, price in prices.items():
            if symbol in self.positions:
                pos = self.positions[symbol]
                pos["current_price"] = price
                pos["market_value"] = price * pos["quantity"]
                pos["unrealized_pnl"] = (price - pos["avg_cost"]) * pos["quantity"]
                self.stop_loss_manager.update_price(symbol, price)

    def update_atr(self, symbol: str, atr: Optional[float]) -> None:
        """
        更新标的最近的 ATR（由外部调度器在每根 bar 调用）。
        下次 track_position 买入时会用到。
        """
        if atr is not None and atr > 0:
            self._recent_atr[symbol] = atr

    def check_stop_loss(self) -> List[TradeSignal]:
        """检查止损止盈"""
        return self.stop_loss_manager.check_all()

    def _update_position_buy(self, symbol: str, quantity: int, price: float):
        """更新买入后的持仓"""
        if symbol in self.positions:
            pos = self.positions[symbol]
            total_qty = pos["quantity"] + quantity
            total_cost = pos["avg_cost"] * pos["quantity"] + price * quantity
            pos["avg_cost"] = total_cost / total_qty
            pos["quantity"] = total_qty
        else:
            self.positions[symbol] = {
                "quantity": quantity,
                "avg_cost": price,
                "current_price": price,
                "market_value": price * quantity,
                "unrealized_pnl": 0,
            }

    def _update_position_sell(self, symbol: str, quantity: int):
        """更新卖出后的持仓"""
        if symbol in self.positions:
            pos = self.positions[symbol]
            pos["quantity"] -= quantity
            if pos["quantity"] <= 0:
                del self.positions[symbol]
            else:
                pos["market_value"] = pos["current_price"] * pos["quantity"]

    def take_daily_snapshot(self):
        """记录每日快照"""
        snapshot = {
            "trade_mode": "paper",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "total_assets": self.total_assets,
            "cash": self.cash,
            "market_value": self.market_value,
            "daily_pnl": self.risk_manager._daily_pnl,
            "positions": {s: dict(p) for s, p in self.positions.items()},
        }
        self.daily_snapshots.append(snapshot)

        if self.db:
            self.db.save_daily_performance(snapshot)

        return snapshot

    def get_portfolio_summary(self) -> dict:
        """获取投资组合摘要"""
        return {
            "total_assets": self.total_assets,
            "cash": self.cash,
            "market_value": self.market_value,
            "positions_count": len(self.positions),
            "positions": {
                s: {
                    "qty": p["quantity"],
                    "avg_cost": p["avg_cost"],
                    "current": p.get("current_price", 0),
                    "pnl": p.get("unrealized_pnl", 0),
                    "pnl_pct": (p.get("current_price", p["avg_cost"]) - p["avg_cost"]) / p["avg_cost"]
                    if p["avg_cost"] > 0 else 0,
                }
                for s, p in self.positions.items()
            },
            "total_trades": len(self.trade_history),
            "return_pct": (self.total_assets - self.initial_capital) / self.initial_capital,
        }

    # ================================================================
    # 阶段8 Fix：跨进程状态持久化
    # ================================================================

    def save_state(self) -> bool:
        """
        把当前 cash / positions / StopLossManager / RiskManager 全部写入
        SQLite trading_state 表。每次 execute_signal 末尾和关键操作后调用。
        """
        if self._state_store is None:
            return False
        try:
            self._state_store.set(KEY_PAPER_ACCOUNT, {
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "last_saved_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._state_store.set(KEY_PAPER_POSITIONS, self.positions)
            self._state_store.set(KEY_PAPER_STOPLOSS, self.stop_loss_manager.dump_state())
            self._state_store.set(KEY_PAPER_RISK, self.risk_manager.dump_state())
            return True
        except Exception as e:
            logger.warning(f"[PaperTrader] save_state 失败：{e}")
            return False

    def load_state(self) -> dict:
        """
        从 SQLite 恢复完整状态（启动时 Orchestrator 显式调用）。

        流程：
        1. 读 paper.account → 恢复 cash
        2. 读 paper.positions → 恢复 positions dict
        3. 读 paper.stop_loss → StopLossManager.load_state
        4. 读 paper.risk_state → RiskManager.load_state
        5. 从 DB 的 trade_records 表恢复 OrderManager counter
        6. 从 DB 查当日 trade_history 放入内存（供对账展示）

        Returns:
            dict: 恢复统计，供日志打印
        """
        stats = {
            "account_restored": False,
            "positions_restored": 0,
            "stop_loss_restored": 0,
            "risk_restored": False,
            "order_counter": 0,
            "trade_history_today": 0,
        }

        if self._state_store is None:
            logger.info("[PaperTrader] 无 DB，跳过 load_state")
            return stats

        # 1. account（cash）
        acct = self._state_store.get(KEY_PAPER_ACCOUNT)
        if acct:
            try:
                self.cash = float(acct.get("cash", self.cash))
                # initial_capital 以本次启动传入的为准，避免配置变更后不一致
                stats["account_restored"] = True
            except Exception as e:
                logger.warning(f"[PaperTrader] account 恢复失败：{e}")

        # 2. positions
        pos_state = self._state_store.get(KEY_PAPER_POSITIONS) or {}
        if isinstance(pos_state, dict) and pos_state:
            # 确保数值类型正确
            for sym, p in pos_state.items():
                self.positions[sym] = {
                    "quantity": int(p.get("quantity", 0)),
                    "avg_cost": float(p.get("avg_cost", 0.0)),
                    "current_price": float(p.get("current_price", p.get("avg_cost", 0.0))),
                    "market_value": float(p.get("market_value", 0.0)),
                    "unrealized_pnl": float(p.get("unrealized_pnl", 0.0)),
                }
            stats["positions_restored"] = len(self.positions)

        # 3. StopLossManager
        sl_state = self._state_store.get(KEY_PAPER_STOPLOSS)
        if sl_state:
            stats["stop_loss_restored"] = self.stop_loss_manager.load_state(sl_state)

        # 4. RiskManager
        risk_state = self._state_store.get(KEY_PAPER_RISK)
        if risk_state:
            stats["risk_restored"] = self.risk_manager.load_state(risk_state)

        # 5. OrderManager counter
        stats["order_counter"] = self.order_manager.restore_counter_from_db(
            self.db, trade_mode="paper"
        )

        # 6. 从 DB 恢复当日 trade_history（供对账展示）
        stats["trade_history_today"] = self._load_today_trade_history()

        logger.info(
            f"[PaperTrader] ✅ load_state 完成：cash=${self.cash:,.2f}, "
            f"positions={stats['positions_restored']}, "
            f"sl={stats['stop_loss_restored']}, "
            f"order_counter={stats['order_counter']}"
        )
        return stats

    def _load_today_trade_history(self) -> int:
        """从 trade_records 表读当日记录填充 trade_history，用于对账展示。"""
        if self.db is None:
            return 0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            df = self.db.load_trades(
                trade_mode="paper", start_date=today, end_date=today + "T23:59:59"
            )
            if df is None or df.empty:
                return 0
            # 清空内存再装入（避免重复）
            self.trade_history = []
            for _, row in df.iterrows():
                self.trade_history.append({
                    "order_id": row.get("order_id"),
                    "trade_mode": row.get("trade_mode", "paper"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "quantity": int(row.get("quantity", 0)),
                    "price": float(row.get("price", 0.0)),
                    "commission": float(row.get("commission", 0.0)),
                    "strategy_name": row.get("strategy_name"),
                    "signal_reason": row.get("signal_reason"),
                    "executed_at": row.get("executed_at"),
                })
            return len(self.trade_history)
        except Exception as e:
            logger.warning(f"[PaperTrader] 加载当日 trade_history 失败：{e}")
            return 0

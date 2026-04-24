"""
Live Trader - 实盘交易器
调用长桥 OpenAPI 执行真实下单。

⚠️ 警告: 此模块涉及真实资金交易，请务必:
1. 先用 PaperTrader 充分验证策略
2. 确认风控参数合理
3. 以最小金额启动
"""

from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger

from src.data.longport_client import LongPortClient
from src.data.database import DatabaseManager
from src.risk.risk_manager import RiskManager
from src.risk.stop_loss import StopLossManager
from src.strategy.base_strategy import TradeSignal, Signal
from src.trader.order_manager import OrderManager, OrderStatus


class LiveTrader:
    """
    实盘交易器。
    
    通过长桥 OpenAPI 执行真实交易。
    """

    def __init__(
        self,
        client: Optional[LongPortClient] = None,
        risk_config: Optional[dict] = None,
        db: Optional[DatabaseManager] = None,
    ):
        self.client = client or LongPortClient()
        self.risk_manager = RiskManager(risk_config or {})
        # 阶段2起：StopLossManager 接收完整 risk_config（含 per_strategy_overrides）
        self.stop_loss_manager = StopLossManager(risk_config or {})
        self.order_manager = OrderManager()
        self.db = db

        self._confirmed = False
        self.trade_history: List[dict] = []
        # 缓存最近的 ATR（订单成交回调时传给 track_position）
        self._recent_atr: Dict[str, float] = {}

        logger.info("LiveTrader initialized (NOT YET CONFIRMED)")

    def confirm_live_trading(self):
        """
        确认开启实盘交易。
        必须显式调用此方法后才能下单。
        """
        self._confirmed = True
        logger.warning("🔴 LIVE TRADING CONFIRMED - Real money will be used!")

    def execute_signal(self, signal: TradeSignal) -> bool:
        """
        执行交易信号（实盘）。
        """
        if not self._confirmed:
            logger.error(
                "Live trading not confirmed! Call confirm_live_trading() first."
            )
            return False

        # 获取账户信息
        try:
            balances = self.client.get_account_balance()
            positions_df = self.client.get_positions()
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return False

        # 构建账户信息
        total_cash = sum(b.get("total_cash", 0) for b in balances)
        available_cash = sum(b.get("available_cash", 0) for b in balances)
        
        current_positions = {}
        if not positions_df.empty:
            for _, row in positions_df.iterrows():
                current_positions[row["symbol"]] = {
                    "quantity": row["quantity"],
                    "avg_cost": row["cost_price"],
                    "market_value": row.get("market_value", row["quantity"] * row["cost_price"]),
                }

        # 估算总资产
        market_val = sum(p.get("market_value", 0) for p in current_positions.values())
        account_info = {
            "total_assets": total_cash + market_val,
            "available_cash": available_cash,
        }

        # 风控校验
        risk_result = self.risk_manager.check_order(
            signal, account_info, current_positions
        )
        if not risk_result.passed:
            logger.warning(f"Live order rejected by risk: {risk_result}")
            return False

        quantity = risk_result.approved_quantity
        if quantity <= 0:
            return False

        # 创建订单记录
        order = self.order_manager.create_order(
            symbol=signal.symbol,
            side=signal.signal.value.lower(),
            quantity=quantity,
            order_type="limit" if signal.price else "market",
            price=signal.price,
            strategy_name=signal.strategy_name,
            signal_reason=signal.reason,
            trade_mode="live",
        )

        # 提交到长桥
        try:
            real_order_id = self.client.submit_order(
                symbol=signal.symbol,
                side=signal.signal.value.lower(),
                quantity=quantity,
                order_type="limit" if signal.price else "market",
                price=signal.price,
            )

            self.order_manager.update_status(order.order_id, OrderStatus.SUBMITTED)
            order.order_id = real_order_id  # 使用真实订单ID

            logger.info(
                f"🔴 LIVE ORDER SUBMITTED: {signal.signal.value} {quantity} "
                f"{signal.symbol} @ {signal.price or 'MKT'}, "
                f"order_id={real_order_id}"
            )

            # 记录交易
            trade_record = {
                "order_id": real_order_id,
                "trade_mode": "live",
                "symbol": signal.symbol,
                "side": signal.signal.value.lower(),
                "quantity": quantity,
                "price": signal.price or 0,
                "strategy_name": signal.strategy_name,
                "signal_reason": signal.reason,
                "executed_at": datetime.now().isoformat(),
            }
            self.trade_history.append(trade_record)
            if self.db:
                self.db.save_trade(trade_record)

            return True

        except Exception as e:
            logger.error(f"Failed to submit live order: {e}")
            self.order_manager.update_status(order.order_id, OrderStatus.FAILED)
            return False

    def cancel_order(self, order_id: str) -> bool:
        """撤销实盘订单"""
        try:
            self.client.cancel_order(order_id)
            self.order_manager.update_status(order_id, OrderStatus.CANCELLED)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def sync_positions(self) -> Dict:
        """同步长桥的实际持仓到本地"""
        try:
            positions_df = self.client.get_positions()
            positions = {}
            for _, row in positions_df.iterrows():
                positions[row["symbol"]] = {
                    "quantity": row["quantity"],
                    "available_quantity": row["available_quantity"],
                    "cost_price": row["cost_price"],
                }
            return positions
        except Exception as e:
            logger.error(f"Failed to sync positions: {e}")
            return {}

    def update_atr(self, symbol: str, atr: Optional[float]) -> None:
        """
        更新标的最近的 ATR（由外部调度器在每根 bar 调用）。
        成交后将用于 StopLossManager.track_position（atr_442 模式）。
        """
        if atr is not None and atr > 0:
            self._recent_atr[symbol] = atr

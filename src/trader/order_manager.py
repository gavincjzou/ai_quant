"""
Order Manager - 订单管理器
统一管理订单生命周期：创建、提交、状态跟踪、成交确认。
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from loguru import logger


class OrderStatus(Enum):
    PENDING = "PENDING"          # 待提交
    SUBMITTED = "SUBMITTED"      # 已提交
    PARTIAL_FILLED = "PARTIAL"   # 部分成交
    FILLED = "FILLED"           # 完全成交
    CANCELLED = "CANCELLED"     # 已撤销
    REJECTED = "REJECTED"       # 被拒绝
    FAILED = "FAILED"           # 失败


@dataclass
class Order:
    """订单数据类"""
    order_id: str = ""
    symbol: str = ""
    side: str = ""               # buy | sell
    quantity: int = 0
    order_type: str = "limit"    # limit | market
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    strategy_name: str = ""
    signal_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    trade_mode: str = "paper"    # paper | live

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED)

    @property
    def is_done(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FAILED)


class OrderManager:
    """订单管理器"""

    def __init__(self):
        self._orders: Dict[str, Order] = {}
        self._order_counter: int = 0

    def restore_counter_from_db(self, db, trade_mode: str = "paper") -> int:
        """
        阶段8 Fix：跨进程持久化支持。
        从 DB 中查询最大 order_id 的编号，把 _order_counter 设置到该值，
        避免每次重启 counter 从 0 开始导致 order_id 冲突。

        order_id 格式：`{trade_mode}_{counter:06d}`，例如 `paper_000007`
        """
        if db is None:
            return 0
        try:
            with db._get_conn() as conn:
                cur = conn.execute(
                    "SELECT order_id FROM trade_records "
                    "WHERE trade_mode = ? AND order_id LIKE ? "
                    "ORDER BY id DESC LIMIT 1",
                    (trade_mode, f"{trade_mode}_%"),
                )
                row = cur.fetchone()
                if not row:
                    return 0
                # 解析 counter 部分
                oid = row[0] if not isinstance(row, sqlite3.Row) else row["order_id"]
                parts = str(oid).split("_")
                if len(parts) >= 2 and parts[-1].isdigit():
                    n = int(parts[-1])
                    self._order_counter = max(self._order_counter, n)
                    logger.info(
                        f"[OrderManager] 从 DB 恢复 counter = {self._order_counter}"
                    )
                    return n
        except Exception as e:
            logger.warning(f"[OrderManager] 从 DB 恢复 counter 失败：{e}")
        return 0

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "limit",
        price: Optional[float] = None,
        strategy_name: str = "",
        signal_reason: str = "",
        trade_mode: str = "paper",
    ) -> Order:
        """
        创建新订单。
        
        Returns:
            Order 实例
        """
        self._order_counter += 1
        order_id = f"{trade_mode}_{self._order_counter:06d}"
        now = datetime.now().isoformat()

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            status=OrderStatus.PENDING,
            strategy_name=strategy_name,
            signal_reason=signal_reason,
            created_at=now,
            updated_at=now,
            trade_mode=trade_mode,
        )

        self._orders[order_id] = order
        logger.info(
            f"Order created: {order_id} | {side} {quantity} {symbol} "
            f"@ {price or 'MKT'} ({trade_mode})"
        )
        return order

    def update_status(
        self,
        order_id: str,
        status: OrderStatus,
        filled_quantity: int = 0,
        filled_price: float = 0.0,
        commission: float = 0.0,
    ):
        """更新订单状态"""
        if order_id not in self._orders:
            logger.warning(f"Unknown order_id: {order_id}")
            return

        order = self._orders[order_id]
        old_status = order.status
        order.status = status
        order.updated_at = datetime.now().isoformat()

        if filled_quantity > 0:
            order.filled_quantity = filled_quantity
            order.filled_price = filled_price
            order.commission = commission

        logger.info(
            f"Order {order_id} status: {old_status.value} -> {status.value}"
        )

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self._orders.get(order_id)

    def get_active_orders(self) -> List[Order]:
        """获取所有活跃订单"""
        return [o for o in self._orders.values() if o.is_active]

    def get_filled_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """获取已成交订单"""
        orders = [o for o in self._orders.values() if o.status == OrderStatus.FILLED]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    def cancel_all(self) -> List[str]:
        """撤销所有活跃订单"""
        cancelled = []
        for order_id, order in self._orders.items():
            if order.is_active:
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.now().isoformat()
                cancelled.append(order_id)
        logger.info(f"Cancelled {len(cancelled)} orders")
        return cancelled

    def get_summary(self) -> Dict:
        """获取订单汇总"""
        total = len(self._orders)
        by_status = {}
        for order in self._orders.values():
            status = order.status.value
            by_status[status] = by_status.get(status, 0) + 1

        return {"total_orders": total, "by_status": by_status}

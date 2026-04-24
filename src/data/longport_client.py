"""
LongPort API Client - 长桥 API 客户端封装
聚焦美股市场，提供行情查询、历史K线、实时订阅等统一接口。
"""

import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv
from loguru import logger

try:
    from longport.openapi import (
        Config,
        QuoteContext,
        TradeContext,
        Period,
        AdjustType,
        SubType,
        OrderSide,
        OrderType,
        TimeInForceType,
        Market,
    )

    LONGPORT_AVAILABLE = True
except ImportError:
    LONGPORT_AVAILABLE = False
    logger.warning("longport SDK not installed. Run: pip install longport")


class LongPortClient:
    """长桥 API 客户端，封装行情与交易接口"""

    def __init__(self, config: Optional[dict] = None):
        """
        初始化客户端。
        
        Args:
            config: 可选配置字典，包含 throttle、retry 等参数
        """
        load_dotenv()
        self._config = config or {}
        self._quote_ctx: Optional["QuoteContext"] = None
        self._trade_ctx: Optional["TradeContext"] = None
        self._last_request_time = 0.0
        self._throttle_interval = 1.0 / self._config.get(
            "throttle_requests_per_second", 5
        )
        self._retry_max = self._config.get("retry_max_attempts", 3)
        self._retry_delay = self._config.get("retry_delay_seconds", 2)

    # ----------------------------------------------------------
    # Connection
    # ----------------------------------------------------------

    def _get_sdk_config(self) -> "Config":
        """从环境变量构建 LongPort SDK 配置"""
        if not LONGPORT_AVAILABLE:
            raise RuntimeError("longport SDK not installed")
        return Config.from_env()

    def connect_quote(self) -> "QuoteContext":
        """建立行情连接"""
        if self._quote_ctx is None:
            cfg = self._get_sdk_config()
            self._quote_ctx = QuoteContext(cfg)
            logger.info("LongPort QuoteContext connected")
        return self._quote_ctx

    def connect_trade(self) -> "TradeContext":
        """建立交易连接"""
        if self._trade_ctx is None:
            cfg = self._get_sdk_config()
            self._trade_ctx = TradeContext(cfg)
            logger.info("LongPort TradeContext connected")
        return self._trade_ctx

    # ----------------------------------------------------------
    # Throttle & Retry
    # ----------------------------------------------------------

    def _throttle(self):
        """请求节流，避免超出 API 频率限制"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._throttle_interval:
            time.sleep(self._throttle_interval - elapsed)
        self._last_request_time = time.time()

    def _retry(self, func, *args, **kwargs):
        """带重试的 API 调用"""
        for attempt in range(1, self._retry_max + 1):
            try:
                self._throttle()
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"API call failed (attempt {attempt}/{self._retry_max}): {e}"
                )
                if attempt == self._retry_max:
                    raise
                time.sleep(self._retry_delay * attempt)

    # ----------------------------------------------------------
    # Quote API - 行情接口
    # ----------------------------------------------------------

    def get_realtime_quote(self, symbols: List[str]) -> pd.DataFrame:
        """
        获取实时行情快照。
        
        Args:
            symbols: 标的代码列表，如 ["AAPL.US", "MSFT.US"]
            
        Returns:
            DataFrame with columns: symbol, last_price, open, high, low, 
            volume, turnover, timestamp
        """
        ctx = self.connect_quote()
        resp = self._retry(ctx.quote, symbols)

        records = []
        for q in resp:
            records.append(
                {
                    "symbol": q.symbol,
                    "last_price": float(q.last_done),
                    "open": float(q.open),
                    "high": float(q.high),
                    "low": float(q.low),
                    "volume": int(q.volume),
                    "turnover": float(q.turnover),
                    "timestamp": q.timestamp,
                }
            )
        return pd.DataFrame(records)

    def get_history_kline(
        self,
        symbol: str,
        period: str = "1d",
        count: int = 250,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取历史K线数据。
        
        Args:
            symbol: 标的代码，如 "AAPL.US"
            period: K线周期 - "1d"(日), "1w"(周), "1M"(月),
                    "1m"(1分钟), "5m"(5分钟), "15m", "30m", "60m"
            count: 获取K线数量
            adjust: 复权方式 - "qfq"(前复权), "hfq"(后复权), "none"
            
        Returns:
            DataFrame with columns: date, open, high, low, close, volume, turnover
        """
        ctx = self.connect_quote()

        period_map = {
            "1d": Period.Day,
            "1w": Period.Week,
            "1M": Period.Month,
            "1m": Period.Min_1,
            "5m": Period.Min_5,
            "15m": Period.Min_15,
            "30m": Period.Min_30,
            "60m": Period.Min_60,
        }
        # longport SDK 3.x 已移除 BackwardAdjust（后复权）
        # 明确拒绝 hfq：回测使用后复权会导致价格与实盘脱节，严禁使用
        if adjust == "hfq":
            raise ValueError(
                "hfq (后复权) 已不支持。回测应使用 qfq (前复权) 保证价格与实盘一致。"
            )

        adjust_map = {
            "qfq": AdjustType.ForwardAdjust,
            "none": AdjustType.NoAdjust,
        }

        sdk_period = period_map.get(period, Period.Day)
        sdk_adjust = adjust_map.get(adjust, AdjustType.ForwardAdjust)

        resp = self._retry(
            ctx.candlesticks, symbol, sdk_period, count, sdk_adjust
        )

        records = []
        for bar in resp:
            records.append(
                {
                    "date": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                    "turnover": float(bar.turnover),
                }
            )

        df = pd.DataFrame(records)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
        return df

    def subscribe_quote(
        self, symbols: List[str], callback, sub_types: Optional[List] = None
    ):
        """
        订阅实时行情推送。
        
        Args:
            symbols: 要订阅的标的列表
            callback: 行情回调函数
            sub_types: 订阅类型列表，默认 [SubType.Quote]
        """
        ctx = self.connect_quote()
        if sub_types is None:
            sub_types = [SubType.Quote]

        ctx.set_on_quote(callback)
        self._retry(ctx.subscribe, symbols, sub_types, is_first_push=True)
        logger.info(f"Subscribed to realtime quotes: {symbols}")

    # ----------------------------------------------------------
    # Trade API - 交易接口
    # ----------------------------------------------------------

    def get_account_balance(self) -> Dict:
        """
        查询账户资金余额。
        
        Returns:
            dict with keys: total_cash, available_cash, frozen_cash, 
            market_value, total_assets, currency
        """
        ctx = self.connect_trade()
        resp = self._retry(ctx.account_balance)

        balances = []
        for acct in resp:
            for cash_info in acct.cash_infos:
                balances.append(
                    {
                        "currency": cash_info.currency,
                        "total_cash": float(cash_info.total_cash),
                        "available_cash": float(cash_info.available_cash),
                        "frozen_cash": float(cash_info.frozen_cash),
                        "market_value": float(cash_info.market_value) if hasattr(cash_info, 'market_value') else 0.0,
                    }
                )
        return balances

    def get_positions(self) -> pd.DataFrame:
        """
        查询当前持仓。
        
        Returns:
            DataFrame with columns: symbol, quantity, available_quantity, 
            cost_price, market_value, unrealized_pnl
        """
        ctx = self.connect_trade()
        resp = self._retry(ctx.stock_positions)

        records = []
        for channel in resp.channels:
            for pos in channel.positions:
                records.append(
                    {
                        "symbol": pos.symbol,
                        "quantity": int(pos.quantity),
                        "available_quantity": int(pos.available_quantity),
                        "cost_price": float(pos.cost_price),
                        "currency": pos.currency,
                    }
                )
        return pd.DataFrame(records)

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "limit",
        price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> str:
        """
        提交订单。
        
        Args:
            symbol: 标的代码，如 "AAPL.US"
            side: "buy" | "sell"
            quantity: 数量
            order_type: "limit" | "market"
            price: 限价单价格（市价单无需）
            time_in_force: "day" | "gtc"
            
        Returns:
            订单ID
        """
        ctx = self.connect_trade()

        side_map = {"buy": OrderSide.Buy, "sell": OrderSide.Sell}
        type_map = {
            "limit": OrderType.LO,     # Limit Order
            "market": OrderType.MO,    # Market Order
        }
        tif_map = {
            "day": TimeInForceType.Day,
            "gtc": TimeInForceType.GoodTilCanceled,
        }

        sdk_side = side_map[side.lower()]
        sdk_type = type_map.get(order_type, OrderType.LO)
        sdk_tif = tif_map.get(time_in_force, TimeInForceType.Day)

        kwargs = {
            "symbol": symbol,
            "order_type": sdk_type,
            "side": sdk_side,
            "submitted_quantity": quantity,
            "time_in_force": sdk_tif,
        }
        if price is not None and order_type == "limit":
            kwargs["submitted_price"] = price

        resp = self._retry(ctx.submit_order, **kwargs)
        order_id = resp.order_id
        logger.info(
            f"Order submitted: {side} {quantity} {symbol} @ {price or 'MKT'}, "
            f"order_id={order_id}"
        )
        return order_id

    def cancel_order(self, order_id: str):
        """撤销订单"""
        ctx = self.connect_trade()
        self._retry(ctx.cancel_order, order_id)
        logger.info(f"Order cancelled: {order_id}")

    def get_today_orders(self) -> pd.DataFrame:
        """查询当日订单"""
        ctx = self.connect_trade()
        resp = self._retry(ctx.today_orders)

        records = []
        for order in resp:
            records.append(
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "side": str(order.side),
                    "quantity": int(order.quantity),
                    "price": float(order.price) if order.price else None,
                    "executed_quantity": int(order.executed_quantity),
                    "status": str(order.status),
                    "created_at": order.created_at,
                }
            )
        return pd.DataFrame(records)

    def get_history_orders(
        self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
    ) -> pd.DataFrame:
        """查询历史订单"""
        ctx = self.connect_trade()
        kwargs = {}
        if start_date:
            kwargs["start_at"] = start_date
        if end_date:
            kwargs["end_at"] = end_date

        resp = self._retry(ctx.history_orders, **kwargs)

        records = []
        for order in resp:
            records.append(
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "side": str(order.side),
                    "quantity": int(order.quantity),
                    "price": float(order.price) if order.price else None,
                    "executed_quantity": int(order.executed_quantity),
                    "status": str(order.status),
                    "created_at": order.created_at,
                }
            )
        return pd.DataFrame(records)

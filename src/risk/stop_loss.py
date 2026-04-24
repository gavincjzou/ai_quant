"""
Stop Loss Manager - 止损止盈管理
跟踪持仓浮动盈亏，触发止损/止盈条件时生成平仓信号。

两套模式：
- legacy:  固定百分比止损 + 固定百分比止盈 + 追踪止损（原有逻辑保留）
- atr_442: ATR 动态止损 + 4-4-2 分批止盈（TP1 40% → TP2 40% → TP3 20%）
           TP1 触发后止损上移到入场价（保本）

关键设计：
- track_position 签名改为 keyword-only atr/strategy_name，旧调用走 legacy 分支
- PositionTracker 扩展 442 字段（tp1/tp2/tp3 触发标志、剩余仓位、止损上移）
- reset() 方法给回测用，避免多次 run 之间状态串台
- check_all 可能返回 partial_sell 动作，回测引擎需处理
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from src.strategy.base_strategy import Signal, TradeSignal


@dataclass
class PositionTracker:
    """单个持仓的追踪信息。"""

    symbol: str
    quantity: int                          # 初始总股数
    avg_cost: float
    highest_price: float = 0.0             # 入场以来最高价（追踪止损用）
    current_price: float = 0.0
    unrealized_pnl_pct: float = 0.0

    # ATR / 442 模式专属
    atr_at_entry: Optional[float] = None   # 入场时 ATR，用于动态止损计算
    strategy_name: Optional[str] = None    # 用于查 per_strategy_overrides
    current_stop: Optional[float] = None   # 当前止损价（绝对价格）
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
    tp3_price: Optional[float] = None
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    tp3_triggered: bool = False
    stop_moved_to_breakeven: bool = False
    remaining_size: int = 0                # 剩余仓位（分批平仓后递减）

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_cost) * self.remaining_size


class StopLossManager:
    """止损止盈管理器。"""

    def __init__(self, config: dict):
        """
        Args:
            config: risk.yaml 完整 dict（包含 stop_loss 段和 per_strategy_overrides）
        """
        self._root_config = config or {}
        sl_cfg = self._root_config.get("stop_loss", self._root_config)

        self._mode = sl_cfg.get("mode", "legacy")

        # legacy 参数
        self._stop_loss_pct = sl_cfg.get("per_trade_stop_loss_pct", 0.05)
        self._take_profit_pct = sl_cfg.get("per_trade_take_profit_pct", 0.15)
        self._trailing_enabled = sl_cfg.get("trailing_stop_enabled", True)
        self._trailing_pct = sl_cfg.get("trailing_stop_pct", 0.05)

        # atr_442 参数
        self._atr_442_cfg = sl_cfg.get("atr_442", {}) or {}

        # 按策略覆盖
        self._per_strategy: dict = (
            self._root_config.get("per_strategy_overrides", {}) or {}
        )

        # 持仓追踪
        self._positions: Dict[str, PositionTracker] = {}

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------

    def reset(self) -> None:
        """清空所有持仓追踪。每次回测开始前必须调用，避免状态串台。"""
        n = len(self._positions)
        self._positions.clear()
        if n > 0:
            logger.debug(f"StopLossManager reset: cleared {n} positions")

    # ------------------------------------------------------------
    # 阶段8 Fix：跨进程持久化（配合 Paper Trading 日扫模式）
    # ------------------------------------------------------------

    def dump_state(self) -> dict:
        """
        序列化所有持仓追踪状态为可 JSON 化的 dict。

        供 PaperTrader.save_state() 调用，写入 trading_state KV 表。
        """
        return {
            "mode": self._mode,
            "positions": {
                sym: {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "highest_price": p.highest_price,
                    "current_price": p.current_price,
                    "unrealized_pnl_pct": p.unrealized_pnl_pct,
                    "atr_at_entry": p.atr_at_entry,
                    "strategy_name": p.strategy_name,
                    "current_stop": p.current_stop,
                    "tp1_price": p.tp1_price,
                    "tp2_price": p.tp2_price,
                    "tp3_price": p.tp3_price,
                    "tp1_triggered": p.tp1_triggered,
                    "tp2_triggered": p.tp2_triggered,
                    "tp3_triggered": p.tp3_triggered,
                    "stop_moved_to_breakeven": p.stop_moved_to_breakeven,
                    "remaining_size": p.remaining_size,
                }
                for sym, p in self._positions.items()
            },
        }

    def load_state(self, state: Optional[dict]) -> int:
        """
        从 dump_state 输出恢复持仓追踪。

        返回恢复的持仓数量。state 为 None 或空则不做任何操作。
        注意：mode 以当前 config 为准，不从 state 覆盖（允许配置热切换）。
        """
        if not state or not state.get("positions"):
            return 0

        self._positions.clear()
        for sym, d in state["positions"].items():
            tracker = PositionTracker(
                symbol=d["symbol"],
                quantity=int(d["quantity"]),
                avg_cost=float(d["avg_cost"]),
                highest_price=float(d.get("highest_price", d["avg_cost"])),
                current_price=float(d.get("current_price", d["avg_cost"])),
                unrealized_pnl_pct=float(d.get("unrealized_pnl_pct", 0.0)),
                atr_at_entry=d.get("atr_at_entry"),
                strategy_name=d.get("strategy_name"),
                current_stop=d.get("current_stop"),
                tp1_price=d.get("tp1_price"),
                tp2_price=d.get("tp2_price"),
                tp3_price=d.get("tp3_price"),
                tp1_triggered=bool(d.get("tp1_triggered", False)),
                tp2_triggered=bool(d.get("tp2_triggered", False)),
                tp3_triggered=bool(d.get("tp3_triggered", False)),
                stop_moved_to_breakeven=bool(d.get("stop_moved_to_breakeven", False)),
                remaining_size=int(d.get("remaining_size", d["quantity"])),
            )
            self._positions[sym] = tracker

        logger.info(
            f"[StopLossManager] 从持久化恢复 {len(self._positions)} 个追踪持仓"
        )
        return len(self._positions)

    # ------------------------------------------------------------
    # 持仓管理
    # ------------------------------------------------------------

    def track_position(
        self,
        symbol: str,
        entry_price: float,
        size: int,
        *,
        atr: Optional[float] = None,
        strategy_name: Optional[str] = None,
    ) -> None:
        """开始追踪新持仓。

        Args:
            symbol: 标的代码
            entry_price: 入场价格（已含滑点）
            size: 开仓股数
            atr: 入场时 ATR（atr_442 模式需要）
            strategy_name: 策略名（用于 per_strategy_overrides）
        """
        tracker = PositionTracker(
            symbol=symbol,
            quantity=size,
            avg_cost=entry_price,
            highest_price=entry_price,
            current_price=entry_price,
            atr_at_entry=atr,
            strategy_name=strategy_name,
            remaining_size=size,
        )

        # atr_442 模式：预计算止损/TP 价位
        if self._mode == "atr_442" and atr is not None and atr > 0:
            overrides = self._per_strategy.get(strategy_name or "", {}) or {}
            stop_mult = overrides.get(
                "atr_stop_mult", self._atr_442_cfg.get("atr_stop_mult", 2.0)
            )
            tp1_rr = overrides.get("tp1_rr", self._atr_442_cfg.get("tp1_rr", 1.0))
            tp2_rr = overrides.get("tp2_rr", self._atr_442_cfg.get("tp2_rr", 2.0))
            tp3_rr = overrides.get("tp3_rr", self._atr_442_cfg.get("tp3_rr", 3.0))

            stop_distance = atr * stop_mult
            tracker.current_stop = entry_price - stop_distance
            tracker.tp1_price = entry_price + atr * tp1_rr
            tracker.tp2_price = entry_price + atr * tp2_rr
            tracker.tp3_price = entry_price + atr * tp3_rr

            logger.debug(
                f"[442] {symbol} entry={entry_price:.2f} ATR={atr:.2f} "
                f"stop={tracker.current_stop:.2f} "
                f"TP1={tracker.tp1_price:.2f} TP2={tracker.tp2_price:.2f} "
                f"TP3={tracker.tp3_price:.2f}"
            )

        self._positions[symbol] = tracker
        logger.debug(
            f"Tracking position: {symbol} qty={size} entry={entry_price:.2f} "
            f"mode={self._mode}"
        )

    def update_price(self, symbol: str, current_price: float) -> None:
        """更新持仓最新价格。"""
        if symbol not in self._positions:
            return

        pos = self._positions[symbol]
        pos.current_price = current_price
        if pos.avg_cost > 0:
            pos.unrealized_pnl_pct = (current_price - pos.avg_cost) / pos.avg_cost
        if current_price > pos.highest_price:
            pos.highest_price = current_price

    def remove_position(self, symbol: str) -> None:
        """移除持仓追踪（完全平仓后调用）。"""
        self._positions.pop(symbol, None)

    def reduce_position(self, symbol: str, reduce_size: int) -> None:
        """减少剩余仓位（部分平仓后调用，不移除）。"""
        if symbol not in self._positions:
            return
        pos = self._positions[symbol]
        pos.remaining_size = max(0, pos.remaining_size - reduce_size)

    # ------------------------------------------------------------
    # 止损止盈检查
    # ------------------------------------------------------------

    def check_all(self) -> List[TradeSignal]:
        """检查所有持仓，返回应触发的平仓信号列表。"""
        signals: List[TradeSignal] = []
        for symbol, pos in list(self._positions.items()):
            if pos.remaining_size <= 0:
                continue

            if self._mode == "atr_442" and pos.atr_at_entry is not None:
                sig = self._check_single_442(pos)
            else:
                sig = self._check_single_legacy(pos)

            if sig:
                signals.append(sig)
        return signals

    # ------------------------------------------------------------
    # legacy 检查（保留原逻辑）
    # ------------------------------------------------------------

    def _check_single_legacy(self, pos: PositionTracker) -> Optional[TradeSignal]:
        """legacy: 固定百分比止损/止盈 + 追踪止损。"""
        pnl_pct = pos.unrealized_pnl_pct

        # 1. 固定止损
        if pnl_pct <= -self._stop_loss_pct:
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=pos.current_price,
                quantity=pos.remaining_size,
                reason=f"Stop loss triggered: {pnl_pct:.2%} <= -{self._stop_loss_pct:.2%}",
                confidence=1.0,
                strategy_name="risk_stop_loss",
            )

        # 2. 固定止盈
        if pnl_pct >= self._take_profit_pct:
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=pos.current_price,
                quantity=pos.remaining_size,
                reason=f"Take profit triggered: {pnl_pct:.2%} >= {self._take_profit_pct:.2%}",
                confidence=1.0,
                strategy_name="risk_take_profit",
            )

        # 3. 追踪止损
        if self._trailing_enabled and pos.highest_price > pos.avg_cost:
            trail_pct = (pos.highest_price - pos.current_price) / pos.highest_price
            if trail_pct >= self._trailing_pct:
                return TradeSignal(
                    symbol=pos.symbol,
                    signal=Signal.SELL,
                    price=pos.current_price,
                    quantity=pos.remaining_size,
                    reason=(
                        f"Trailing stop: dropped {trail_pct:.2%} from high "
                        f"${pos.highest_price:.2f}"
                    ),
                    confidence=1.0,
                    strategy_name="risk_trailing_stop",
                )

        return None

    # ------------------------------------------------------------
    # atr_442 检查（新逻辑）
    # ------------------------------------------------------------

    def _check_single_442(self, pos: PositionTracker) -> Optional[TradeSignal]:
        """atr_442: ATR 动态止损 + 4-4-2 分批止盈。

        触发顺序（每 bar 只触发一个动作，next() 会反复调用推进）：
            1. current_price <= current_stop  -> 止损全平
            2. TP1 未触发 + 价 >= TP1 -> 平 40% + 止损上移到保本
            3. TP2 未触发 + 价 >= TP2 -> 平 40%
            4. TP3 未触发 + 价 >= TP3 -> 平剩余 20%，清仓

        关键：TP 从近到远检查，让 TP1 优先于 TP3 触发（否则跳空会跳过 TP1）。
        """
        if pos.remaining_size <= 0:
            return None

        price = pos.current_price
        orig_qty = pos.quantity

        tp1_size_pct = self._atr_442_cfg.get("tp1_size_pct", 0.40)
        tp2_size_pct = self._atr_442_cfg.get("tp2_size_pct", 0.40)
        move_to_be = self._atr_442_cfg.get("move_stop_to_breakeven_after_tp1", True)

        # --- 1. 止损 ---
        if pos.current_stop is not None and price <= pos.current_stop:
            stop_reason = (
                "Stop loss (ATR)"
                if not pos.stop_moved_to_breakeven
                else "Stop @ breakeven"
            )
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=price,
                quantity=pos.remaining_size,
                reason=f"{stop_reason}: {price:.2f} <= {pos.current_stop:.2f}",
                confidence=1.0,
                strategy_name="risk_442_stop",
            )

        # --- 2. TP1（最近档，必须先检查）---
        if (
            not pos.tp1_triggered
            and pos.tp1_price is not None
            and price >= pos.tp1_price
        ):
            pos.tp1_triggered = True
            qty = max(1, int(orig_qty * tp1_size_pct))
            qty = min(qty, pos.remaining_size)
            # 止损上移到入场价（保本）
            if move_to_be and pos.avg_cost > (pos.current_stop or 0):
                pos.current_stop = pos.avg_cost
                pos.stop_moved_to_breakeven = True
                logger.debug(
                    f"[442] {pos.symbol} TP1 hit -> move stop to breakeven "
                    f"@ {pos.avg_cost:.2f}"
                )
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=price,
                quantity=qty,
                reason=f"TP1: {price:.2f} >= {pos.tp1_price:.2f}",
                confidence=1.0,
                strategy_name="risk_442_tp1",
            )

        # --- 3. TP2（只有 TP1 已触发后才检查）---
        if (
            pos.tp1_triggered
            and not pos.tp2_triggered
            and pos.tp2_price is not None
            and price >= pos.tp2_price
        ):
            pos.tp2_triggered = True
            qty = max(1, int(orig_qty * tp2_size_pct))
            qty = min(qty, pos.remaining_size)
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=price,
                quantity=qty,
                reason=f"TP2: {price:.2f} >= {pos.tp2_price:.2f}",
                confidence=1.0,
                strategy_name="risk_442_tp2",
            )

        # --- 4. TP3（TP2 已触发后才检查，清仓）---
        if (
            pos.tp2_triggered
            and not pos.tp3_triggered
            and pos.tp3_price is not None
            and price >= pos.tp3_price
        ):
            pos.tp3_triggered = True
            return TradeSignal(
                symbol=pos.symbol,
                signal=Signal.SELL,
                price=price,
                quantity=pos.remaining_size,  # 清仓
                reason=f"TP3: {price:.2f} >= {pos.tp3_price:.2f}",
                confidence=1.0,
                strategy_name="risk_442_tp3",
            )

        return None

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------

    def get_position_status(self) -> List[Dict]:
        """获取所有持仓的风控状态。"""
        statuses = []
        for symbol, pos in self._positions.items():
            trail_pct = 0.0
            if pos.highest_price > 0:
                trail_pct = (pos.highest_price - pos.current_price) / pos.highest_price

            statuses.append({
                "symbol": symbol,
                "mode": self._mode,
                "strategy_name": pos.strategy_name,
                "quantity": pos.quantity,
                "remaining_size": pos.remaining_size,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "pnl_pct": pos.unrealized_pnl_pct,
                "highest_price": pos.highest_price,
                "trail_from_high_pct": trail_pct,
                "current_stop": pos.current_stop,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "tp3_price": pos.tp3_price,
                "tp1_triggered": pos.tp1_triggered,
                "tp2_triggered": pos.tp2_triggered,
                "tp3_triggered": pos.tp3_triggered,
                "stop_moved_to_breakeven": pos.stop_moved_to_breakeven,
            })
        return statuses

    def get_position(self, symbol: str) -> Optional[PositionTracker]:
        """获取指定标的的持仓 tracker（只读）。"""
        return self._positions.get(symbol)

"""
Risk Manager - 风控管理器
交易执行前的统一风控校验入口。
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

from loguru import logger

from src.strategy.base_strategy import TradeSignal, Signal


@dataclass
class RiskCheckResult:
    """风控校验结果"""
    passed: bool                         # 是否通过风控
    original_quantity: int = 0           # 原始申请数量
    approved_quantity: int = 0           # 批准数量
    approved_price: float = 0.0          # 批准价格
    rejected_reasons: List[str] = field(default_factory=list)

    def __str__(self):
        if self.passed:
            return f"PASS: qty={self.approved_quantity}"
        return f"REJECTED: {', '.join(self.rejected_reasons)}"


class RiskManager:
    """
    风控管理器。
    
    风控规则（串联执行，任一拦截则阻止交易）：
    1. 单笔仓位限制
    2. 最大持仓数量
    3. 单日亏损限额
    4. 组合回撤熔断
    5. 财报日窗口限制
    """

    def __init__(self, config: dict):
        """
        Args:
            config: risk.yaml 完整配置
        """
        self._config = config
        self._pos_cfg = config.get("position", {})
        self._sl_cfg = config.get("stop_loss", {})
        self._daily_cfg = config.get("daily_limits", {})
        self._port_cfg = config.get("portfolio_limits", {})

        # 运行时状态
        self._daily_pnl: float = 0.0
        self._daily_trade_count: int = 0
        self._daily_date: Optional[date] = None
        self._peak_value: float = 0.0
        self._is_circuit_breaker: bool = False

        # 财报日历缓存 {symbol: [date, ...]}
        self._earnings_dates: Dict[str, List[date]] = {}

    # ----------------------------------------------------------
    # Core Check
    # ----------------------------------------------------------

    def check_order(
        self,
        signal: TradeSignal,
        account_info: dict,
        current_positions: dict,
    ) -> RiskCheckResult:
        """
        交易前统一风控校验。
        
        Args:
            signal: 交易信号
            account_info: 账户信息 {total_assets, available_cash, ...}
            current_positions: 当前持仓 {symbol: {quantity, market_value}}
            
        Returns:
            RiskCheckResult
        """
        reasons = []
        total_assets = account_info.get("total_assets", 0)
        available_cash = account_info.get("available_cash", 0)

        # 重置每日计数器
        today = date.today()
        if self._daily_date != today:
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
            self._daily_date = today

        # 只对买入信号做严格检查
        if signal.signal != Signal.BUY:
            # 卖出直接放行（清仓/止损/熔断后清仓 都不应被风控拦截）
            # 阶段 11 P1-7 修复：SELL 放行检查放在熔断检查之前
            qty = signal.quantity or current_positions.get(signal.symbol, {}).get("quantity", 0)
            return RiskCheckResult(
                passed=True,
                original_quantity=qty,
                approved_quantity=qty,
                approved_price=signal.price,
            )

        # 0. 熔断检查（仅拦截买入；SELL 已在上面放行）
        if self._is_circuit_breaker:
            return RiskCheckResult(
                passed=False,
                rejected_reasons=["Circuit breaker active: max drawdown exceeded"],
            )

        # 1. 单日交易次数限制
        max_daily_trades = self._daily_cfg.get("max_daily_trades", 10)
        if self._daily_trade_count >= max_daily_trades:
            reasons.append(f"Daily trade limit reached: {max_daily_trades}")

        # 2. 单日亏损限额
        max_daily_loss = total_assets * self._daily_cfg.get("max_daily_loss_pct", 0.03)
        if self._daily_pnl < -max_daily_loss:
            reasons.append(
                f"Daily loss limit: ${self._daily_pnl:.2f} exceeds -${max_daily_loss:.2f}"
            )

        # 3. 最大持仓数量
        max_positions = self._pos_cfg.get("max_total_positions", 5)
        current_count = len([v for v in current_positions.values() if v.get("quantity", 0) > 0])
        if signal.symbol not in current_positions and current_count >= max_positions:
            reasons.append(f"Max positions reached: {current_count}/{max_positions}")

        # 3b. 阶段8 Fix：日线选股策略禁止重复开仓（同一标的有持仓时不加仓）
        # 避免 momentum 信号每天重复触发导致无限加仓
        existing_qty = current_positions.get(signal.symbol, {}).get("quantity", 0)
        allow_add = self._pos_cfg.get("allow_add_to_existing", False)
        if existing_qty > 0 and not allow_add:
            reasons.append(
                f"Already holds {signal.symbol} ({existing_qty} shares), "
                f"no add-on allowed (set position.allow_add_to_existing=true to enable)"
            )

        # 4. 单票最大仓位
        max_single_pct = self._pos_cfg.get("max_single_position_pct", 0.20)
        max_single_amount = total_assets * max_single_pct

        # 计算目标仓位金额
        default_pct = self._pos_cfg.get("default_position_pct", 0.10)
        target_amount = min(total_assets * default_pct, max_single_amount, available_cash)

        # 已有持仓则计算增量
        existing_value = current_positions.get(signal.symbol, {}).get("market_value", 0)
        allowed_amount = max(0, max_single_amount - existing_value)
        target_amount = min(target_amount, allowed_amount)

        if target_amount <= 0:
            reasons.append(
                f"Position limit: {signal.symbol} at max ({max_single_pct:.0%} of assets)"
            )

        # 4b. 阶段 11 P1-2 新增：行业集中度限制
        # 防止 V1 Top-N 全是同行业（如半导体）时一次性买入导致单行业过度暴露
        max_sector_pct = self._port_cfg.get("max_sector_concentration", 0)
        max_industry_pct = self._port_cfg.get("max_industry_concentration", 0)
        if (max_sector_pct > 0 or max_industry_pct > 0) and target_amount > 0:
            sec, ind = self._lookup_sector_industry(signal.symbol)
            if sec or ind:
                # 算"加上 target_amount 后"该 sector/industry 的总暴露
                sec_exposure_after, ind_exposure_after = self._compute_sector_exposure(
                    signal.symbol, target_amount, current_positions, total_assets, sec, ind,
                )
                if max_sector_pct > 0 and sec and sec_exposure_after > max_sector_pct:
                    reasons.append(
                        f"Sector concentration: '{sec}' would be {sec_exposure_after:.1%} "
                        f"after this trade > {max_sector_pct:.0%} limit"
                    )
                if max_industry_pct > 0 and ind and ind_exposure_after > max_industry_pct:
                    reasons.append(
                        f"Industry concentration: '{ind}' would be {ind_exposure_after:.1%} "
                        f"after this trade > {max_industry_pct:.0%} limit"
                    )

        # 5. 最小下单金额
        min_amount = self._pos_cfg.get("min_order_amount_usd", 50)
        if target_amount < min_amount and not reasons:
            reasons.append(f"Below min order amount: ${target_amount:.2f} < ${min_amount}")

        # 6. 财报日窗口
        block_window = self._port_cfg.get("block_earnings_window_days", 1)
        if block_window > 0 and self._is_near_earnings(signal.symbol, today, block_window):
            reasons.append(
                f"Earnings window: {signal.symbol} has earnings within {block_window} days"
            )

        # 计算批准数量
        if signal.price > 0 and not reasons:
            approved_qty = int(target_amount / signal.price)
            approved_qty = max(approved_qty, 1)  # 美股最少1股
        else:
            approved_qty = 0

        if reasons:
            logger.warning(f"Risk check REJECTED for {signal}: {reasons}")
            return RiskCheckResult(
                passed=False,
                original_quantity=signal.quantity or 0,
                approved_quantity=0,
                rejected_reasons=reasons,
            )

        logger.info(f"Risk check PASSED: {signal.symbol} qty={approved_qty}")
        return RiskCheckResult(
            passed=True,
            original_quantity=signal.quantity or approved_qty,
            approved_quantity=approved_qty,
            approved_price=signal.price,
        )

    # ----------------------------------------------------------
    # State Updates
    # ----------------------------------------------------------

    def update_daily_pnl(self, pnl: float):
        """更新每日盈亏"""
        self._daily_pnl += pnl
        self._daily_trade_count += 1

    def update_portfolio_value(self, current_value: float):
        """更新组合净值，检查回撤熔断"""
        if current_value > self._peak_value:
            self._peak_value = current_value

        if self._peak_value > 0:
            drawdown = (self._peak_value - current_value) / self._peak_value
            max_dd = self._port_cfg.get("max_drawdown_pct", 0.10)
            if drawdown >= max_dd:
                self._is_circuit_breaker = True
                logger.critical(
                    f"CIRCUIT BREAKER: drawdown {drawdown:.2%} >= {max_dd:.2%}, "
                    f"all trading suspended!"
                )

    def reset_circuit_breaker(self):
        """手动解除熔断"""
        self._is_circuit_breaker = False
        logger.info("Circuit breaker manually reset")

    # ----------------------------------------------------------
    # 阶段8 Fix：跨进程持久化
    # ----------------------------------------------------------

    def dump_state(self) -> dict:
        """序列化运行时状态为可 JSON 化的 dict。"""
        return {
            "daily_pnl": self._daily_pnl,
            "daily_trade_count": self._daily_trade_count,
            "daily_date": self._daily_date.isoformat() if self._daily_date else None,
            "peak_value": self._peak_value,
            "is_circuit_breaker": self._is_circuit_breaker,
        }

    def load_state(self, state: Optional[dict]) -> bool:
        """
        从 dump_state 输出恢复状态。state 为 None 或空则不做任何操作。

        特别处理：如果 state 里的 daily_date 不是今天，daily_pnl/daily_trade_count 自动归零
        （check_order 里也有同样逻辑，这里是为了 dump 后语义一致）。
        """
        if not state:
            return False
        try:
            self._daily_pnl = float(state.get("daily_pnl", 0.0))
            self._daily_trade_count = int(state.get("daily_trade_count", 0))
            d = state.get("daily_date")
            self._daily_date = date.fromisoformat(d) if d else None
            self._peak_value = float(state.get("peak_value", 0.0))
            self._is_circuit_breaker = bool(state.get("is_circuit_breaker", False))

            # 如果不是今天的计数器，归零
            today = date.today()
            if self._daily_date != today:
                self._daily_pnl = 0.0
                self._daily_trade_count = 0
                self._daily_date = today

            logger.info(
                f"[RiskManager] 从持久化恢复：peak=${self._peak_value:,.0f}, "
                f"daily_pnl=${self._daily_pnl:,.2f}, "
                f"circuit_breaker={self._is_circuit_breaker}"
            )
            return True
        except Exception as e:
            logger.warning(f"[RiskManager] load_state 失败：{e}")
            return False

    # ----------------------------------------------------------
    # Earnings Calendar
    # ----------------------------------------------------------

    def set_earnings_dates(self, symbol: str, dates: List[date]):
        """设置标的财报日期"""
        self._earnings_dates[symbol] = dates

    def _is_near_earnings(self, symbol: str, today: date, window: int) -> bool:
        """判断是否在财报日窗口内"""
        dates = self._earnings_dates.get(symbol, [])
        for d in dates:
            diff = abs((d - today).days)
            if diff <= window:
                return True
        return False

    # ----------------------------------------------------------
    # 阶段 11 P1-2：行业集中度限制 helpers
    # ----------------------------------------------------------

    # 类级缓存：避免每次 check_order 都查 DB
    _sector_cache: Dict[str, tuple] = {}

    def _lookup_sector_industry(self, symbol: str) -> tuple:
        """查 symbol → (sector, industry)，从 fundamental_ratios 表读。

        失败时返回 (None, None)。失败不报错（让 check 静默跳过该限制）。
        懒加载 + 进程内缓存。
        """
        if symbol in self._sector_cache:
            return self._sector_cache[symbol]

        try:
            # 延迟 import，避免 RiskManager 强依赖 DatabaseManager
            from src.data.database import DatabaseManager
            db = DatabaseManager()
            with db._get_conn() as conn:
                row = conn.execute(
                    "SELECT sector, industry FROM fundamental_ratios WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
            if row:
                result = (row[0], row[1])
            else:
                result = (None, None)
        except Exception as e:
            logger.debug(f"[RiskManager] _lookup_sector_industry({symbol}) 失败: {e}")
            result = (None, None)

        self._sector_cache[symbol] = result
        return result

    def _compute_sector_exposure(
        self,
        new_symbol: str,
        new_amount: float,
        current_positions: dict,
        total_assets: float,
        new_sector: str,
        new_industry: str,
    ) -> tuple:
        """算"加上 new_amount 后" new_symbol 所属 sector / industry 的总暴露占比。

        返回 (sector_pct, industry_pct)，0 表示无该维度数据。
        """
        if total_assets <= 0:
            return (0.0, 0.0)

        sector_value = new_amount  # 新建仓金额
        industry_value = new_amount

        for sym, pos in current_positions.items():
            if sym == new_symbol:
                continue  # 当前 symbol 的已有仓位通过 new_amount 路径已计入（target_amount 已扣 existing）
            mv = float(pos.get("market_value", 0) or 0)
            if mv <= 0:
                continue
            sec, ind = self._lookup_sector_industry(sym)
            if new_sector and sec == new_sector:
                sector_value += mv
            if new_industry and ind == new_industry:
                industry_value += mv

        # 还要把 new_symbol 自己已有的持仓加回（避免双重计算同时漏算）
        existing_self = float(
            current_positions.get(new_symbol, {}).get("market_value", 0) or 0
        )
        sector_value += existing_self
        industry_value += existing_self

        sec_pct = sector_value / total_assets if new_sector else 0.0
        ind_pct = industry_value / total_assets if new_industry else 0.0
        return (sec_pct, ind_pct)

    @classmethod
    def clear_sector_cache(cls):
        """清空 sector 缓存（测试 + sector 数据更新后调用）"""
        cls._sector_cache.clear()

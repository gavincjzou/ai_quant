"""
Daily Reconciliation - 每日收盘对账

盘后（16:05 ET）由 Scheduler 触发：
1. 当日交易汇总（成交次数、买卖比例、按策略/标的分布）
2. 持仓快照（对 PaperTrader 内存持仓 vs DB 持仓）
3. 风控状态（当日 PnL、累计回撤、熔断状态、日限用量）
4. 止损止盈管理器快照（多少仓位处于 TP1/TP2 已触发状态）
5. 生成对账报告 + 触发告警（有异常时）

输出：
- 控制台打印
- output/reconciliation/YYYY-MM-DD.md
- alerts：INFO (正常收盘) / WARNING (有异常) / CRITICAL (对账不平)
"""

import os
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger

from src.data.database import DatabaseManager
from src.monitor.alerts import get_alerter, AlertLevel
from src.risk.risk_manager import RiskManager
from src.risk.stop_loss import StopLossManager
from src.trader.paper_trader import PaperTrader


class DailyReconciliation:
    """每日收盘对账器。"""

    def __init__(
        self,
        trader: PaperTrader,
        db: Optional[DatabaseManager] = None,
        output_dir: str = "output/reconciliation",
    ):
        self.trader = trader
        self.db = db or trader.db
        self.alerter = get_alerter()
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ============================================================

    def run(self, date: Optional[str] = None) -> dict:
        """
        执行完整对账流程。

        Returns:
            dict with keys: date, trades, positions, risk, stop_loss, issues, report_path
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        logger.info(f"[Reconciliation] ▶️ {date}")

        result = {
            "date": date,
            "trades": self._summarize_trades(date),
            "positions": self._summarize_positions(),
            "risk": self._summarize_risk(),
            "stop_loss": self._summarize_stop_loss(),
            "issues": [],
        }

        # 对账校验
        result["issues"] = self._detect_issues(result)

        # 保存快照
        snap = self.trader.take_daily_snapshot()
        result["snapshot"] = snap

        # 生成报告
        report_path = os.path.join(self.output_dir, f"{date}.md")
        self._write_report(report_path, result)
        result["report_path"] = report_path

        # 发送告警
        self._fire_alerts(result)

        logger.info(f"[Reconciliation] ✅ 报告 -> {report_path}")
        return result

    # ============================================================
    # 各子模块汇总
    # ============================================================

    def _summarize_trades(self, date: str) -> dict:
        """当日交易汇总（阶段8 Fix：优先从 DB 查当日交易记录）。"""
        trades: List[dict] = []

        # 优先从 DB 查（跨进程可靠）
        if self.db is not None:
            try:
                df = self.db.load_trades(
                    trade_mode="paper",
                    start_date=date,
                    end_date=date + "T23:59:59",
                )
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        trades.append({
                            "order_id": row.get("order_id"),
                            "symbol": row.get("symbol"),
                            "side": row.get("side"),
                            "quantity": int(row.get("quantity", 0)),
                            "price": float(row.get("price", 0.0)),
                            "commission": float(row.get("commission", 0.0)),
                            "strategy_name": row.get("strategy_name"),
                            "signal_reason": row.get("signal_reason"),
                            "executed_at": row.get("executed_at"),
                        })
            except Exception as e:
                logger.warning(f"[Reconciliation] DB 查当日交易失败，降级用内存：{e}")

        # 兜底：DB 不可用时从 trade_history 内存
        if not trades:
            trades = [
                t for t in self.trader.trade_history
                if t.get("executed_at", "").startswith(date)
            ]

        summary = {
            "total": len(trades),
            "buy_count": sum(1 for t in trades if t.get("side") == "buy"),
            "sell_count": sum(1 for t in trades if t.get("side") == "sell"),
            "total_commission": round(sum(t.get("commission", 0) for t in trades), 2),
            "by_strategy": {},
            "by_symbol": {},
            "trades": trades,
        }
        for t in trades:
            sn = t.get("strategy_name", "unknown")
            summary["by_strategy"][sn] = summary["by_strategy"].get(sn, 0) + 1
            sym = t.get("symbol", "?")
            summary["by_symbol"][sym] = summary["by_symbol"].get(sym, 0) + 1
        return summary

    def _summarize_positions(self) -> dict:
        """持仓快照。"""
        port = self.trader.get_portfolio_summary()
        return {
            "count": port["positions_count"],
            "total_assets": round(port["total_assets"], 2),
            "cash": round(port["cash"], 2),
            "market_value": round(port["market_value"], 2),
            "return_pct": port["return_pct"],
            "positions": port["positions"],
        }

    def _summarize_risk(self) -> dict:
        """风控状态快照（阶段8 Fix：修正字段名匹配 RiskManager 真实属性）。"""
        rm: RiskManager = self.trader.risk_manager
        total_assets = self.trader.total_assets
        peak = getattr(rm, "_peak_value", 0.0) or 0.0

        # 当前回撤（相对 peak）
        max_drawdown = 0.0
        if peak > 0:
            max_drawdown = max(0.0, (peak - total_assets) / peak)

        is_halted = getattr(rm, "_is_circuit_breaker", False)
        halt_reason = (
            f"回撤达到 {max_drawdown:.2%} >= 熔断阈值" if is_halted else ""
        )

        return {
            "daily_pnl": getattr(rm, "_daily_pnl", 0),
            "daily_trade_count": getattr(rm, "_daily_trade_count", 0),
            "daily_pnl_pct": getattr(rm, "_daily_pnl", 0)
            / max(total_assets, 1),
            "peak_value": peak,
            "max_drawdown": max_drawdown,
            "is_halted": is_halted,
            "halt_reason": halt_reason,
        }

    def _summarize_stop_loss(self) -> dict:
        """止损止盈管理器快照。"""
        sl: StopLossManager = self.trader.stop_loss_manager
        positions = getattr(sl, "_positions", {})
        if not positions:
            return {"tracked_count": 0, "detail": []}

        detail = []
        tp1_hit = tp2_hit = tp3_hit = be_moved = 0
        for sym, pos in positions.items():
            detail.append({
                "symbol": sym,
                "entry_price": pos.avg_cost,
                "current_price": pos.current_price,
                "current_stop": pos.current_stop,
                "tp1_triggered": pos.tp1_triggered,
                "tp2_triggered": pos.tp2_triggered,
                "tp3_triggered": pos.tp3_triggered,
                "stop_moved_to_breakeven": pos.stop_moved_to_breakeven,
                "remaining_size": pos.remaining_size,
                "strategy_name": getattr(pos, "strategy_name", ""),
            })
            if pos.tp1_triggered:
                tp1_hit += 1
            if pos.tp2_triggered:
                tp2_hit += 1
            if pos.tp3_triggered:
                tp3_hit += 1
            if pos.stop_moved_to_breakeven:
                be_moved += 1

        return {
            "tracked_count": len(positions),
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
            "breakeven_moved": be_moved,
            "detail": detail,
        }

    # ============================================================
    # 对账校验
    # ============================================================

    def _detect_issues(self, r: dict) -> List[dict]:
        """检测对账异常。"""
        issues = []

        # 1) PaperTrader 持仓 vs StopLossManager 持仓一致性
        trader_syms = set(r["positions"]["positions"].keys())
        sl_syms = set(x["symbol"] for x in r["stop_loss"]["detail"])
        miss_in_sl = trader_syms - sl_syms
        miss_in_trader = sl_syms - trader_syms
        if miss_in_sl:
            issues.append({
                "level": "WARNING",
                "code": "SL_MISSING",
                "msg": f"PaperTrader 有持仓但 StopLossManager 未追踪：{miss_in_sl}",
            })
        if miss_in_trader:
            issues.append({
                "level": "WARNING",
                "code": "SL_STALE",
                "msg": f"StopLossManager 追踪了已平仓标的：{miss_in_trader}",
            })

        # 2) 熔断检查
        if r["risk"]["is_halted"]:
            issues.append({
                "level": "CRITICAL",
                "code": "HALTED",
                "msg": f"组合熔断中：{r['risk']['halt_reason']}",
            })

        # 3) 日亏损超限
        daily_pnl_pct = r["risk"]["daily_pnl_pct"]
        if daily_pnl_pct < -0.02:
            issues.append({
                "level": "WARNING" if daily_pnl_pct > -0.03 else "CRITICAL",
                "code": "DAILY_LOSS",
                "msg": f"当日亏损 {daily_pnl_pct:.2%}，接近/超过 3% 日限",
            })

        return issues

    # ============================================================
    # 报告 + 告警
    # ============================================================

    def _write_report(self, path: str, r: dict):
        """Markdown 对账报告。"""
        lines = [
            f"# 收盘对账 - {r['date']}",
            "",
            "## 1. 资金概览",
            "",
            f"- 总资产：${r['positions']['total_assets']:,.2f}",
            f"- 现金：${r['positions']['cash']:,.2f}",
            f"- 持仓市值：${r['positions']['market_value']:,.2f}",
            f"- 累计收益率：{r['positions']['return_pct']:+.2%}",
            "",
            "## 2. 当日交易",
            "",
            f"- 总成交：{r['trades']['total']} 笔（买 {r['trades']['buy_count']} / 卖 {r['trades']['sell_count']}）",
            f"- 总手续费：${r['trades']['total_commission']:.2f}",
        ]

        if r["trades"]["by_strategy"]:
            lines.append("- 按策略分布：")
            for k, v in r["trades"]["by_strategy"].items():
                lines.append(f"  - {k}: {v} 笔")

        if r["trades"]["by_symbol"]:
            lines.append("- 按标的分布：")
            for k, v in r["trades"]["by_symbol"].items():
                lines.append(f"  - {k}: {v} 笔")

        if r["trades"]["trades"]:
            lines.append("")
            lines.append("### 明细")
            lines.append("")
            lines.append("| 时间 | 标的 | 方向 | 数量 | 成交价 | 策略 | 原因 |")
            lines.append("|---|---|---|---|---|---|---|")
            for t in r["trades"]["trades"]:
                lines.append(
                    f"| {t.get('executed_at','')[:19]} | {t['symbol']} | "
                    f"{t['side'].upper()} | {t['quantity']} | "
                    f"${t['price']:.2f} | {t.get('strategy_name','')} | "
                    f"{t.get('signal_reason','')[:40]} |"
                )

        # 持仓明细
        lines.append("")
        lines.append("## 3. 当前持仓")
        lines.append("")
        if r["positions"]["positions"]:
            lines.append("| 标的 | 股数 | 成本 | 现价 | 浮盈亏 | 浮盈亏% |")
            lines.append("|---|---|---|---|---|---|")
            for sym, pos in r["positions"]["positions"].items():
                lines.append(
                    f"| {sym} | {pos['qty']} | ${pos['avg_cost']:.2f} | "
                    f"${pos['current']:.2f} | ${pos['pnl']:+.2f} | "
                    f"{pos['pnl_pct']:+.2%} |"
                )
        else:
            lines.append("_无持仓_")

        # 止损止盈
        lines.append("")
        lines.append("## 4. 止损止盈追踪")
        lines.append("")
        sl = r["stop_loss"]
        lines.append(
            f"追踪 {sl['tracked_count']} 只，TP1 命中 {sl.get('tp1_hit',0)} | "
            f"TP2 {sl.get('tp2_hit',0)} | TP3 {sl.get('tp3_hit',0)} | "
            f"保本上移 {sl.get('breakeven_moved',0)}"
        )
        if sl["detail"]:
            lines.append("")
            lines.append("| 标的 | 策略 | 入场 | 现价 | 止损 | TP1 | TP2 | TP3 | 保本 | 剩余股数 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for d in sl["detail"]:
                mark = lambda x: "✅" if x else "⬜"  # noqa: E731
                lines.append(
                    f"| {d['symbol']} | {d.get('strategy_name','')} | "
                    f"${d['entry_price']:.2f} | ${d['current_price']:.2f} | "
                    f"${d['current_stop']:.2f} | {mark(d['tp1_triggered'])} | "
                    f"{mark(d['tp2_triggered'])} | {mark(d['tp3_triggered'])} | "
                    f"{mark(d['stop_moved_to_breakeven'])} | {d['remaining_size']} |"
                )

        # 风控
        lines.append("")
        lines.append("## 5. 风控状态")
        lines.append("")
        risk = r["risk"]
        halt = "🔴 HALTED" if risk["is_halted"] else "🟢 OK"
        lines.append(
            f"- 当日 PnL：${risk['daily_pnl']:+.2f} ({risk['daily_pnl_pct']:+.2%})"
        )
        lines.append(f"- 当日交易数：{risk['daily_trade_count']}")
        lines.append(f"- 组合最大回撤：{risk['max_drawdown']:.2%}")
        lines.append(f"- 熔断状态：{halt}")
        if risk["is_halted"]:
            lines.append(f"- 熔断原因：{risk['halt_reason']}")

        # 异常
        lines.append("")
        lines.append("## 6. 对账异常")
        lines.append("")
        if r["issues"]:
            for i in r["issues"]:
                emoji = "🚨" if i["level"] == "CRITICAL" else "⚠️"
                lines.append(f"- {emoji} **[{i['level']}] {i['code']}** — {i['msg']}")
        else:
            lines.append("_无异常，全部校验通过 ✅_")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _fire_alerts(self, r: dict):
        """按异常级别触发告警。"""
        level = AlertLevel.INFO
        for issue in r["issues"]:
            if issue["level"] == "CRITICAL":
                level = AlertLevel.CRITICAL
                break
            elif issue["level"] == "WARNING" and level == AlertLevel.INFO:
                level = AlertLevel.WARNING

        msg_parts = [
            f"📅 {r['date']} 收盘对账完成",
            f"💰 总资产 ${r['positions']['total_assets']:,.2f} "
            f"（{r['positions']['return_pct']:+.2%}）",
            f"📊 当日成交 {r['trades']['total']} 笔，"
            f"手续费 ${r['trades']['total_commission']:.2f}",
            f"📦 持仓 {r['positions']['count']} 只，"
            f"现金 ${r['positions']['cash']:,.2f}",
            f"🛡 当日 PnL {r['risk']['daily_pnl_pct']:+.2%}",
        ]

        if r["issues"]:
            msg_parts.append("")
            msg_parts.append("⚠️ 异常：")
            for i in r["issues"]:
                msg_parts.append(f"  - [{i['level']}] {i['msg']}")

        msg_parts.append("")
        msg_parts.append(f"📄 详细报告：{r.get('report_path','')}")

        self.alerter.send(
            "\n".join(msg_parts),
            level=level,
            title=f"收盘对账 {r['date']}",
            tags=["reconciliation", "daily"],
        )

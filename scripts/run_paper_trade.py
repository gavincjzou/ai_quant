#!/usr/bin/env python3
"""
Paper Trading 启动器（阶段7 日线扫描模式）

职责：
1. 加载配置、初始化 DataFetcher / StrategyManager / PaperTrader
2. 定义四类 Job 回调：pre_market / scan / monitor / post_close
3. 通过 PaperTradingScheduler 编排 APScheduler（daemon 模式）
4. 阶段7 新增：--daily-scan 模式（日线扫描，无需常驻）

用法：
    # 日常推荐（方案 C：开机即跑，日线模式）
    python scripts/run_paper_trade.py --daily-scan

    # 其他模式
    python scripts/run_paper_trade.py --preview       # 展示 Jobs 预览后退出
    python scripts/run_paper_trade.py --once scan     # 手动触发一次 scan（调试）
    python scripts/run_paper_trade.py                  # daemon 长驻（上云时用）
    python scripts/run_paper_trade.py --capital 800000 --once all
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.data.data_fetcher import DataFetcher
from src.data.database import DatabaseManager
from src.data.market_calendar import USMarketCalendar
from src.data.trading_state import TradingState
from src.monitor.alerts import get_alerter
from src.monitor.daily_reconciliation import DailyReconciliation
from src.monitor.logger import setup_logging
from src.strategy.strategy_manager import StrategyManager
from src.trader.paper_trader import PaperTrader
from src.trader.scheduler import PaperTradingScheduler
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import calc_atr


class PaperTradingOrchestrator:
    """串起所有组件的总编排器。"""

    def __init__(self, capital: float, history_source: str = "longport"):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = project_root
        config_loader = ConfigLoader(os.path.join(project_root, "config"))
        self.strategies_cfg = config_loader.get_strategies_config()
        self.risk_cfg = config_loader.get_risk_config()

        self.watchlist: List[str] = self.strategies_cfg.get("watchlist", [])

        # 交易组件
        db_path = os.path.join(project_root, "data_cache", "quant.db")
        self.db = DatabaseManager(db_path)
        self.trading_state = TradingState(db_path)
        self.fetcher = DataFetcher(db=self.db, history_source=history_source)
        self.strategy_mgr = StrategyManager(self.strategies_cfg)
        self.calendar = USMarketCalendar()
        self.alerter = get_alerter()
        # 阶段8 Fix Round 2：PaperTrader 初始化时注入 alerter
        # 这样所有调用路径（scan/monitor/manual_close/未来任何脚本）的成交都会自动告警
        self.trader = PaperTrader(
            initial_capital=capital,
            risk_config=self.risk_cfg,
            db=self.db,
            alerter=self.alerter,
        )
        self.reconciliation = DailyReconciliation(
            trader=self.trader,
            db=self.db,
            output_dir=os.path.join(project_root, "output", "reconciliation"),
        )

        # ATR 周期：从 risk.yaml 取默认
        self._atr_period_default = (
            self.risk_cfg.get("stop_loss", {}).get("atr_442", {}).get("atr_period", 14)
        )

        # 阶段8 Fix：从 SQLite 恢复 PaperTrader 的完整状态（cash/positions/SL/Risk/Orders）
        # 这是让日线扫描模式具备跨进程幂等性的关键
        restore_stats = self.trader.load_state()
        if restore_stats.get("positions_restored", 0) > 0:
            logger.info(
                f"[Orchestrator] 🔄 状态恢复：持仓 {restore_stats['positions_restored']} 只，"
                f"现金 ${self.trader.cash:,.2f}，"
                f"总资产 ${self.trader.total_assets:,.2f}"
            )

        logger.info(
            f"[Orchestrator] 初始化完成：capital=${capital:,.0f}, "
            f"watchlist={len(self.watchlist)} 只，策略={self.strategy_mgr.list_strategies()}"
        )

    # ----------------- 数据拉取 -----------------

    def _refresh_data(self) -> Dict[str, pd.DataFrame]:
        """拉取/加载最新历史数据，返回 {symbol: DataFrame}。"""
        data_map = {}
        for sym in self.watchlist:
            df = self.fetcher.load_data(sym, period="1d")
            if df is not None and not df.empty:
                data_map[sym] = df
        return data_map

    def _update_atrs(self, data_map: Dict[str, pd.DataFrame]):
        """把每个标的的最新 ATR 传给 PaperTrader。"""
        for sym, df in data_map.items():
            atr_series = calc_atr(df, period=self._atr_period_default)
            if atr_series is None or atr_series.empty:
                continue
            last_atr = atr_series.iloc[-1]
            if pd.notna(last_atr):
                self.trader.update_atr(sym, float(last_atr))

    # ============================================================
    # Job 1: 盘前
    # ============================================================

    def pre_market(self):
        """盘前：拉数据 + 健康检查。"""
        data_map = self._refresh_data()
        missing = [s for s in self.watchlist if s not in data_map]
        if missing:
            self.alerter.warning(
                f"盘前数据缺失：{missing}",
                title="数据健康检查",
                tags=["pre_market"],
            )
        else:
            self.alerter.info(
                f"✅ 盘前数据就绪：{len(data_map)} 只标的",
                title="盘前准备",
                tags=["pre_market"],
            )
        self._update_atrs(data_map)

    # ============================================================
    # Job 2: 盘中信号扫描
    # ============================================================

    def scan(self):
        """策略信号扫描 + 执行下单。"""
        data_map = self._refresh_data()
        if not data_map:
            logger.warning("[scan] 无可用数据，跳过")
            return

        self._update_atrs(data_map)

        # 跑策略
        signals_map = self.strategy_mgr.run_watchlist(self.watchlist, data_map)

        executed = 0
        for sym, signals in signals_map.items():
            for sig in signals:
                before = self.trader.positions.get(sym, {}).get("quantity", 0)
                ok = self.trader.execute_signal(sig)
                if ok:
                    executed += 1
                    # 阶段8 Fix Round 2：告警已下沉到 PaperTrader.execute_signal
                    # 这里不再重复发 alerter.info（否则会双推送）

        # 更新持仓价格 + 检查止损止盈
        prices = {s: df["close"].iloc[-1] for s, df in data_map.items() if not df.empty}
        self.trader.update_prices(prices)

        stop_signals = self.trader.check_stop_loss()
        for sig in stop_signals:
            ok = self.trader.execute_signal(sig)
            # 告警由 PaperTrader._fire_trade_alert 自动发（risk_ 前缀会走 WARNING 级别）

        logger.info(f"[scan] ✅ 完成，执行 {executed} 笔订单，"
                    f"止损/止盈触发 {len(stop_signals)} 个")

        # 阶段8 Fix：scan 结束后持久化（价格变化也要写回）
        self.trader.save_state()

    # ============================================================
    # Job 3: 盘中监控（仅止损止盈检查）
    # ============================================================

    def monitor(self):
        """盘中监控：只更新持仓价格 + 检查止损止盈，不产生新信号。"""
        data_map = self._refresh_data()
        if not data_map:
            return
        prices = {s: df["close"].iloc[-1] for s, df in data_map.items() if not df.empty}
        self.trader.update_prices(prices)
        stops = self.trader.check_stop_loss()
        for sig in stops:
            self.trader.execute_signal(sig)
            # 告警由 PaperTrader._fire_trade_alert 自动发

        # 阶段8 Fix：monitor 结束后持久化
        self.trader.save_state()

    # ============================================================
    # Job 4: 盘后对账
    # ============================================================

    def post_close(self):
        """盘后：跑对账 + 生成报告 + 发送每日摘要。"""
        return self.reconciliation.run()

    # ============================================================
    # 阶段11 P0-1（dev-13）：刷持仓现价
    # ============================================================

    def _refresh_position_prices(self) -> dict:
        """
        拉持仓最新 K 线 → 写回 kline_data → 调 trader.update_prices 刷
        positions[sym].current_price / market_value / unrealized_pnl
        以及 stop_loss_manager 内部的现价。

        失败策略：单只失败不抛异常，记 warn 继续；返回 {ok, fail, prices}。
        被 daily-scan 在每个 gap 日 scan 之前调用。
        """
        positions = list(self.trader.positions.keys())
        result = {"ok": [], "fail": [], "prices": {}}

        if not positions:
            logger.info("[RefreshPrices] 当前无持仓，跳过")
            return result

        logger.info(f"[RefreshPrices] 刷新 {len(positions)} 只持仓现价：{positions}")

        try:
            # count=5 给点冗余以防节假日 / 停牌；同时写回 kline_data
            fetched = self.fetcher.fetch_history(
                symbols=positions,
                period="1d",
                count=5,
                save_to_db=True,
            )
        except Exception as e:
            # 整体失败（如 LongPort 完全不通），记 error 但不抛
            logger.exception(f"[RefreshPrices] 批量拉取失败：{e}")
            result["fail"] = positions
            return result

        prices = {}
        for sym in positions:
            df = fetched.get(sym)
            if df is None or df.empty:
                logger.warning(f"[RefreshPrices] {sym} 拉到空数据，跳过")
                result["fail"].append(sym)
                continue
            try:
                # 取最后一根的 close（pandas Series → float）
                last_close = float(df["close"].iloc[-1])
                if last_close <= 0:
                    logger.warning(f"[RefreshPrices] {sym} close={last_close} 异常，跳过")
                    result["fail"].append(sym)
                    continue
                prices[sym] = last_close
                result["ok"].append(sym)
            except Exception as e:
                logger.warning(f"[RefreshPrices] {sym} 取 close 失败：{e}")
                result["fail"].append(sym)

        if prices:
            self.trader.update_prices(prices)
            result["prices"] = prices
            logger.info(f"[RefreshPrices] ✅ 已更新 {len(prices)} 只现价：{prices}")
        else:
            logger.warning("[RefreshPrices] ⚠️ 所有持仓刷价均失败")

        return result

    # ============================================================
    # 阶段7 新增：日线扫描模式（方案 C）
    # ============================================================

    def daily_scan(self, backfill_limit_days: int = 14, dry_run: bool = False) -> dict:
        """
        日线扫描模式（方案 C）：
        1. 检查 last_scan_date，计算截至"已收盘交易日"的 gap
        2. 对每个 gap 日：scan（信号扫描+执行）+ post_close（对账）
        3. 更新 last_scan_date
        4. 推送"日扫摘要"到企业微信

        严格模式：只处理美东已过 16:30 收盘缓冲的交易日。

        Args:
            backfill_limit_days: 最多回补多少个交易日（防止首次运行跑太多）
            dry_run: True 时只计算 gap 不执行

        Returns:
            {
                "last_scan_before": "YYYY-MM-DD" or None,
                "target_date": "YYYY-MM-DD",
                "gap_days": [...],
                "processed": [...],
                "skipped_reason": None or str,
            }
        """
        logger.info("=" * 60)
        logger.info("[DailyScan] ▶️ 开始日线扫描（方案 C）")

        target = self.calendar.last_closed_trading_day()
        last_scan = self.trading_state.get_last_scan_date()

        summary = {
            "last_scan_before": last_scan.isoformat() if last_scan else None,
            "target_date": target.isoformat(),
            "gap_days": [],
            "processed": [],
            "skipped_reason": None,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

        # 计算 gap
        if last_scan is None:
            # 首次运行：只跑目标日本身，避免首次拉取几年数据
            gap = [target]
        elif target <= last_scan:
            summary["skipped_reason"] = (
                f"当前已收盘最近交易日 {target} 不晚于 last_scan {last_scan}，无需补跑"
            )
            logger.info(f"[DailyScan] ⏸️ {summary['skipped_reason']}")
            self._send_daily_scan_summary(summary, dry_run=dry_run)
            return summary
        else:
            gap = self.calendar.trading_days_between(
                last_scan + timedelta(days=1), target
            )
            # 限制最多补跑
            if len(gap) > backfill_limit_days:
                logger.warning(
                    f"[DailyScan] gap={len(gap)} 天超过 limit={backfill_limit_days}，"
                    f"只处理最后 {backfill_limit_days} 天"
                )
                gap = gap[-backfill_limit_days:]

        summary["gap_days"] = [d.isoformat() for d in gap]
        logger.info(f"[DailyScan] last_scan={last_scan} → target={target}, gap={len(gap)} 天")

        if dry_run:
            logger.info("[DailyScan] dry_run=True，不执行实际扫描")
            self._send_daily_scan_summary(summary, dry_run=True)
            return summary

        # 执行每一天
        for d in gap:
            try:
                logger.info(f"[DailyScan] ▶️ 处理 {d}")
                # 阶段11 P0-1（dev-13）：scan 之前先刷持仓现价
                # 修前 bug：take_daily_snapshot 读 positions[sym].current_price
                #          但 current_price 只在 _update_position_buy 时设过一次
                #          导致 8 天 daily_perf 数据完全相同（市值永远是开仓价）
                # 修法：拉持仓 5 只最新 K 线 → update_prices → 同步 kline_data
                self._refresh_position_prices()
                # 日线策略：scan 用当前 DB 里的数据（已包含 d 日收盘）
                self.scan()
                recon = self.reconciliation.run(date=d.isoformat())
                # 阶段8 Fix Round 3：每天补跑完都写一份净值快照
                # 这样 daily_performance 表会随 Paper Trading 累积，
                # Dashboard 的净值曲线才有真实数据
                self.trader.take_daily_snapshot(scan_date=d.isoformat())
                summary["processed"].append({
                    "date": d.isoformat(),
                    "trades": recon["trades"]["total"],
                    "positions": recon["positions"]["count"],
                    "return_pct": recon["positions"]["return_pct"],
                    "issues": len(recon["issues"]),
                })
                self.trading_state.set_last_scan_date(d)
            except Exception as e:
                logger.exception(f"[DailyScan] {d} 处理失败：{e}")
                summary["processed"].append({
                    "date": d.isoformat(),
                    "error": str(e),
                })

        self.trading_state.touch_last_run()
        self.trading_state.save_scan_summary(summary)

        logger.info(f"[DailyScan] ✅ 完成，处理 {len(summary['processed'])} 天")
        self._send_daily_scan_summary(summary, dry_run=False)
        return summary

    def _send_daily_scan_summary(self, summary: dict, dry_run: bool = False):
        """推送日扫摘要到企业微信（markdown 格式）。

        两种模式：
        - 0 gap（skipped_reason 非空）：心跳模式，含当前 ET/CN 时间 + 持仓 + 下次时间
        - 有 gap：原有摘要表格 + 各日期处理结果
        """
        title = "📊 日线扫描" + ("（Dry Run）" if dry_run else "")
        skipped = summary.get("skipped_reason")

        if skipped:
            # ========== 0 gap 心跳模式（增强版）==========
            try:
                import pytz
                et_tz = pytz.timezone("America/New_York")
                cn_tz = pytz.timezone("Asia/Shanghai")
                now_utc = datetime.now(pytz.UTC)
                et_now = now_utc.astimezone(et_tz).strftime("%Y-%m-%d %H:%M %Z")
                cn_now = now_utc.astimezone(cn_tz).strftime("%Y-%m-%d %H:%M CST")
            except Exception:
                et_now = "(N/A)"
                cn_now = datetime.now().strftime("%Y-%m-%d %H:%M")

            # 读账户和持仓
            try:
                acct = self.trading_state.get("paper.account") or {}
                positions = self.trading_state.get("paper.positions") or {}
                cash = float(acct.get("cash", 0))
                market_value = sum(
                    float(p.get("market_value", 0)) for p in positions.values()
                )
                total_assets = cash + market_value
            except Exception as e:
                logger.warning(f"[心跳] 读 trading_state 失败: {e}")
                acct, positions, cash, market_value, total_assets = {}, {}, 0, 0, 0

            lines = [f"### 💚 {title} - 心跳"]
            lines.append("")
            lines.append(f"> 美东 `{et_now}` · 北京 `{cn_now}`")
            lines.append("")
            lines.append("**📊 系统状态**")
            lines.append(f"- 总资产：${total_assets:,.2f}")
            lines.append(f"- 现金：${cash:,.2f}")
            lines.append(f"- 持仓市值：${market_value:,.2f}（{len(positions)} 只）")
            lines.append("")
            lines.append("**ℹ️ 跳过原因**")
            lines.append(f"> {skipped}")
            lines.append("")

            # 持仓 Top-5（按浮盈% 倒序）
            if positions:
                pos_with_pct = []
                for sym, p in positions.items():
                    cost_basis = float(p.get("avg_cost", 0)) * float(p.get("quantity", 0))
                    upnl = float(p.get("unrealized_pnl", 0))
                    pct = upnl / cost_basis if cost_basis > 0 else 0.0
                    pos_with_pct.append((sym, p, pct))
                pos_with_pct.sort(key=lambda x: x[2], reverse=True)

                lines.append("**📦 当前持仓 Top-5**")
                lines.append("| Symbol | 数量 | 现价 | 浮盈% |")
                lines.append("|---|---|---|---|")
                for sym, p, pct in pos_with_pct[:5]:
                    qty = int(p.get("quantity", 0))
                    cur = float(p.get("current_price", 0))
                    emoji = "🟢" if pct > 0 else ("🔴" if pct < -0.005 else "⚪")
                    lines.append(f"| {sym} | {qty} | ${cur:.2f} | {emoji} {pct:+.2%} |")
                lines.append("")

            lines.append("**⏰ 下次运行**")
            lines.append("- 每天北京 `08:00`（≈ 美东盘后 4h）")
            lines.append("")
            lines.append(f"_{summary.get('started_at', '')}_")

            text_version = (
                f"{title} - 心跳 | 总资产 ${total_assets:,.0f} · "
                f"持仓 {len(positions)} 只 · {skipped}"
            )
            self.alerter.info(
                text_version,
                title="日线扫描心跳",
                tags=["daily_scan", "heartbeat"],
                markdown="\n".join(lines),
            )
            return

        # ========== 有 gap 处理：原逻辑 ==========
        lines = [f"### {title}"]
        lines.append(f"- 上次扫描：`{summary['last_scan_before'] or '(首次)'}`")
        lines.append(f"- 目标日期：`{summary['target_date']}`")
        lines.append(f"- Gap 天数：{len(summary['gap_days'])}")

        if summary["processed"]:
            lines.append("")
            lines.append("| 日期 | 成交 | 持仓 | 累计收益 | 异常 |")
            lines.append("|---|---|---|---|---|")
            for p in summary["processed"]:
                if "error" in p:
                    lines.append(f"| {p['date']} | ❌ | - | - | {p['error'][:20]} |")
                else:
                    lines.append(
                        f"| {p['date']} | {p['trades']} | {p['positions']} | "
                        f"{p['return_pct']:+.2%} | {p['issues']} |"
                    )

        lines.append("")
        lines.append(f"_{summary.get('started_at', '')}_")

        # text 版本给非 markdown 通道用
        text_version = f"{title}\n" + "\n".join(
            line for line in lines[1:] if not line.startswith("|") and not line.startswith("##")
        )

        level_from_processed = any(
            isinstance(p, dict) and "error" in p for p in summary.get("processed", [])
        )
        if level_from_processed:
            self.alerter.warning(
                text_version,
                title="日线扫描有异常",
                tags=["daily_scan"],
                markdown="\n".join(lines),
            )
        else:
            self.alerter.info(
                text_version,
                title="日线扫描完成",
                tags=["daily_scan"],
                markdown="\n".join(lines),
            )


def main():
    parser = argparse.ArgumentParser(description="Paper Trading 启动器")
    parser.add_argument("--capital", type=float, default=800000,
                        help="初始资金（默认 HKD 80万）")
    parser.add_argument("--preview", action="store_true",
                        help="展示 Scheduler Jobs 预览后退出")
    parser.add_argument("--once", type=str, default=None,
                        choices=["pre_market", "scan", "monitor", "post_close", "all"],
                        help="手动触发一次指定 Job 后退出")
    parser.add_argument("--daily-scan", action="store_true",
                        help="方案 C：日线扫描模式（开机即跑，无需常驻）")
    parser.add_argument("--dry-run", action="store_true",
                        help="配合 --daily-scan 使用：只计算 gap 不执行")
    parser.add_argument("--backfill-limit", type=int, default=14,
                        help="--daily-scan 最多回补的交易日数（默认 14）")
    parser.add_argument("--source", type=str, default="longport",
                        choices=["longport", "yfinance"])
    args = parser.parse_args()

    # 配置日志
    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs",
    )
    setup_logging(log_dir=log_dir, log_level="INFO")

    orch = PaperTradingOrchestrator(
        capital=args.capital, history_source=args.source
    )

    # ===== 阶段7 新增：日线扫描模式（独立入口，不走 scheduler） =====
    if args.daily_scan:
        print(f"\n🔍 Daily Scan 模式启动 | 当前 ET: "
              f"{datetime.now().astimezone(orch.calendar.ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"告警通道：{'WeCom✅' if orch.alerter.wecom_ready else 'WeCom❌'} | "
              f"{'Email✅' if orch.alerter.email_ready else 'Email❌'} | Log✅")
        print("-" * 80)
        summary = orch.daily_scan(
            backfill_limit_days=args.backfill_limit,
            dry_run=args.dry_run,
        )
        print(f"\n✅ Daily Scan 完成")
        print(f"  last_scan_before: {summary['last_scan_before']}")
        print(f"  target_date:      {summary['target_date']}")
        print(f"  gap_days:         {len(summary['gap_days'])}")
        print(f"  processed:        {len(summary['processed'])}")
        if summary.get("skipped_reason"):
            print(f"  skipped:          {summary['skipped_reason']}")

        # 阶段9 V1 Fix：Daily Scan 完成 + 有新数据处理 → 自动跑 V1 因子 + 推送
        # 触发条件：有 gap 被处理（len(processed) > 0），避免周末无数据时重复跑
        # 失败不影响主流程
        import subprocess
        proj_root = orch.project_root

        v1_ok = False
        has_processed = bool(summary.get("processed"))

        if not args.dry_run and has_processed:
            v1_err_detail = ""
            try:
                print("\n🎯 自动跑 V1 多因子打分...")
                result = subprocess.run(
                    [sys.executable, os.path.join(proj_root, "scripts", "run_factor_screen.py"),
                     "--version", "v1"],
                    cwd=proj_root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    print(f"  ✅ V1 因子快照已更新 → output/factor_screen_*_v1.md")
                    v1_ok = True
                else:
                    v1_err_detail = result.stderr[-300:]
                    print(f"  ⚠️ V1 因子失败（不影响主流程）：{v1_err_detail}")
            except Exception as e:
                v1_err_detail = f"{type(e).__name__}: {e}"
                logger.warning(f"[DailyScan] V1 因子失败（忽略）：{e}")

            # 阶段11 P0-2（dev-13）：V1 失败时推企微告警，不再静默吞
            # 避免再次出现 8 天 V1 全失败但没人发现的情况
            if not v1_ok and v1_err_detail:
                try:
                    is_socket_token = "socket/token" in v1_err_detail
                    title = (
                        "⚠️ V1 因子打分失败（socket token 闪断）"
                        if is_socket_token else
                        "⚠️ V1 因子打分失败"
                    )
                    body_lines = [
                        f"**时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        f"**类型**：{'LongPort socket/token Connect error' if is_socket_token else '其他异常'}",
                        f"**详情**：```\n{v1_err_detail[-200:]}\n```",
                        "",
                        "**影响**：周报 / 业绩归因 / Forward Backtest 本次跳过",
                        "**建议**：手动 `./run_daily.sh` 重跑，或检查 LongPort SDK 状态",
                    ]
                    orch.alerter.send(
                        level="warning",
                        title=title,
                        body="\n".join(body_lines),
                        tags=["daily_scan", "v1_factor_fail"],
                    )
                except Exception as ae:
                    logger.warning(f"[DailyScan] V1 失败告警发送失败（忽略）：{ae}")

            # V1 成功 → 推送排名变化（阶段9 V1 集成 Phase C）
            if v1_ok:
                try:
                    from src.factor.factor_notifier import FactorNotifier
                    from src.monitor.alerts import get_alerter
                    print("\n📣 推送 V1 Top-10 + 排名变化...")
                    notifier = FactorNotifier(orch.db, get_alerter())
                    pushed = notifier.notify()
                    if pushed:
                        print(f"  ✅ V1 排名推送完成")
                    else:
                        print(f"  ℹ️ V1 推送跳过（可能未配 WeCom webhook 或无变化）")
                except Exception as e:
                    logger.warning(f"[DailyScan] V1 排名推送失败（忽略）：{e}")
                    print(f"  ⚠️ V1 推送失败（不影响主流程）：{e}")
        elif not has_processed:
            print("\n⏸️ 无 gap 处理，跳过 V1 因子（周末/无新数据时正常行为）")

        # 阶段10 A3：周度调仓分析报告（仅周一触发，即处理周五数据时）
        # target_date.weekday() == 4 表示处理的是周五数据，意味着这是周一/周二早上跑的
        # 设计：每周 1 次推送，避免每天刷屏
        if not args.dry_run and v1_ok:
            try:
                from datetime import date as _date
                target_str = summary.get("target_date")
                target_date_obj = None
                if target_str:
                    try:
                        target_date_obj = _date.fromisoformat(target_str)
                    except Exception:
                        target_date_obj = None

                # 周五 weekday() = 4
                if target_date_obj and target_date_obj.weekday() == 4:
                    print("\n📅 检测到处理周五数据 → 触发周度调仓分析...")
                    result = subprocess.run(
                        [sys.executable,
                         os.path.join(proj_root, "scripts", "weekly_rotation_report.py"),
                         "--date", target_str],
                        cwd=proj_root,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    if result.returncode == 0:
                        print(f"  ✅ 周度调仓报告已生成 + 推送")
                    else:
                        print(f"  ⚠️ 周度报告失败（不影响主流程）：{result.stderr[-200:]}")

                    # 阶段 11 P1-4：周五数据时同时跑业绩归因
                    print("\n📈 周五数据触发 → 跑业绩归因报告...")
                    result2 = subprocess.run(
                        [sys.executable,
                         os.path.join(proj_root, "scripts", "attribution_report.py")],
                        cwd=proj_root,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if result2.returncode == 0:
                        print(f"  ✅ 业绩归因报告已生成 + 推送")
                    else:
                        print(f"  ⚠️ 业绩归因失败（不影响主流程）：{result2.stderr[-200:]}")

                    # 阶段 11 P1-5：周五数据时同时跑 Forward Backtest
                    print("\n🔬 周五数据触发 → 跑 Forward Backtest...")
                    result3 = subprocess.run(
                        [sys.executable,
                         os.path.join(proj_root, "scripts", "forward_backtest_factor.py"),
                         "--version", "v1", "--top", "5", "--days", "30"],
                        cwd=proj_root,
                        capture_output=True,
                        text=True,
                        timeout=300,  # forward 涉及多个 snapshot × 多标的 K 线
                    )
                    if result3.returncode == 0:
                        print(f"  ✅ Forward Backtest 报告已生成 + 推送")
                    else:
                        print(f"  ⚠️ Forward Backtest 失败（不影响主流程）：{result3.stderr[-200:]}")
                else:
                    weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
                        target_date_obj.weekday()
                    ] if target_date_obj else "未知"
                    print(f"\nℹ️ 本次处理 {weekday_name} 数据，跳过周报（仅周五数据触发）")
            except Exception as e:
                logger.warning(f"[DailyScan] 周度调仓报告失败（忽略）：{e}")
                print(f"  ⚠️ 周报失败（不影响主流程）：{e}")

        # 阶段8 Fix：Daily Scan 完成后自动生成 Dashboard
        # 必须放在 V1 之后，以便 Dashboard 读到最新 V1 snapshot
        # 失败不影响主流程（只 log warning）
        if not args.dry_run:
            try:
                print("\n📊 自动生成 Dashboard...")
                result = subprocess.run(
                    [sys.executable, os.path.join(proj_root, "scripts", "build_dashboard.py")],
                    cwd=proj_root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    print(f"  ✅ Dashboard 已更新 → output/dashboard.html")
                else:
                    print(f"  ⚠️ Dashboard 生成失败（不影响主流程）：{result.stderr[-200:]}")
            except Exception as e:
                logger.warning(f"[DailyScan] Dashboard 生成失败（忽略）：{e}")

        return

    # ===== 原有模式：scheduler preview / once / daemon =====
    scheduler = PaperTradingScheduler(
        pre_market_fn=orch.pre_market,
        scan_fn=orch.scan,
        monitor_fn=orch.monitor,
        post_close_fn=orch.post_close,
        calendar=orch.calendar,
        monitor_interval_minutes=30,
    )

    jobs = scheduler.list_jobs()
    print("\n🕒 Scheduler Jobs 预览：")
    print("-" * 80)
    for j in jobs:
        print(f"  {j['id']:<22} | {j['name']:<20} | next: {j['next_run_time']}")
    print("-" * 80)
    print(f"当前 ET: {datetime.now().astimezone(orch.calendar.ET)}")
    print(f"监控频率：每 30 分钟")
    print(f"告警通道：{'WeCom✅' if orch.alerter.wecom_ready else 'WeCom❌'} | "
          f"{'Telegram✅' if orch.alerter.telegram_ready else 'Telegram❌'} | "
          f"{'Email✅' if orch.alerter.email_ready else 'Email❌'} | Log✅")
    print()

    if args.preview:
        print("ℹ️ --preview 模式，退出。")
        return

    if args.once:
        if args.once == "all":
            orch.pre_market()
            orch.scan()
            orch.monitor()
            orch.post_close()
        else:
            job_fn = {
                "pre_market": orch.pre_market,
                "scan": orch.scan,
                "monitor": orch.monitor,
                "post_close": orch.post_close,
            }[args.once]
            job_fn()
        print(f"\n✅ --once {args.once} 完成，退出。")
        return

    # 正式启动（daemon）
    scheduler.start()


if __name__ == "__main__":
    main()

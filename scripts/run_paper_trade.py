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
                # 日线策略：scan 用当前 DB 里的数据（已包含 d 日收盘）
                self.scan()
                recon = self.reconciliation.run(date=d.isoformat())
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
        """推送日扫摘要到企业微信（markdown 格式）。"""
        title = "📊 日线扫描" + ("（Dry Run）" if dry_run else "")
        lines = [f"### {title}"]
        lines.append(f"- 上次扫描：`{summary['last_scan_before'] or '(首次)'}`")
        lines.append(f"- 目标日期：`{summary['target_date']}`")
        lines.append(f"- Gap 天数：{len(summary['gap_days'])}")

        if summary.get("skipped_reason"):
            lines.append("")
            lines.append(f"> ℹ️ {summary['skipped_reason']}")
        elif summary["processed"]:
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

        # 阶段8 Fix：Daily Scan 完成后自动生成 Dashboard
        # 失败不影响主流程（只 log warning）
        if not args.dry_run:
            try:
                print("\n📊 自动生成 Dashboard...")
                import subprocess
                proj_root = orch.project_root
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

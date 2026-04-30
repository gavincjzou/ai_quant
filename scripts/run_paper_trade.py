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
from typing import Dict, List, Optional

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
    # 阶段11 P1-1（dev-13）：数据陈旧度告警
    # ============================================================

    def _check_data_staleness(self) -> dict:
        """
        检测三类数据的陈旧度，触发企微告警避免再次"卡住没人知道"。

        检查项：
        1. daily_performance：最近 N 条 market_value 是否完全相同（精度 $1）
        2. kline_data：持仓最新 K 线日期距今天 > 阈值
        3. factor_snapshots：V1 最新 snapshot 距今天 > 阈值

        告警阈值（从 risk_cfg 读，默认值见下）：
        - perf_stale_days = 3（连续 3 天 market_value 不变即告警）
        - kline_stale_days = 3
        - factor_stale_days = 5（V1 周末不跑，留点冗余）
        """
        from datetime import date, datetime as _dt

        cfg = self.risk_cfg.get("staleness_check", {}) if self.risk_cfg else {}
        perf_stale_days = int(cfg.get("perf_stale_days", 3))
        kline_stale_days = int(cfg.get("kline_stale_days", 3))
        factor_stale_days = int(cfg.get("factor_stale_days", 5))

        issues = []
        details = {}

        # ---- 1. daily_performance market_value 是否卡住 ----
        try:
            with self.db._get_conn() as conn:
                rows = conn.execute(
                    "SELECT date, market_value FROM daily_performance "
                    "WHERE trade_mode='paper' ORDER BY date DESC LIMIT ?",
                    (perf_stale_days,),
                ).fetchall()
            if len(rows) >= perf_stale_days:
                mvs = [float(r[1]) for r in rows]
                # 全部差异 < $1 视为"完全相同"
                spread = max(mvs) - min(mvs)
                details["perf"] = {
                    "rows_checked": len(rows),
                    "spread": spread,
                    "latest_dates": [r[0] for r in rows],
                }
                if spread < 1.0:
                    issues.append(
                        f"daily_performance 最近 {perf_stale_days} 天 market_value "
                        f"完全相同（spread < \\$1，可能 current_price 未刷新）"
                    )
        except Exception as e:
            logger.warning(f"[Staleness] daily_perf 检查失败：{e}")

        # ---- 2. kline_data 持仓最新 K 线日期 ----
        try:
            positions = list(self.trader.positions.keys())
            today = date.today()
            if positions:
                placeholders = ",".join(["?"] * len(positions))
                with self.db._get_conn() as conn:
                    rows = conn.execute(
                        f"SELECT symbol, MAX(date) FROM kline_data "
                        f"WHERE symbol IN ({placeholders}) GROUP BY symbol",
                        positions,
                    ).fetchall()
                stale_symbols = []
                kline_dates = {}
                for sym, max_date in rows:
                    if not max_date:
                        continue
                    # max_date 形如 '2026-04-29 12:00:00' 或 '2026-04-29'
                    d_str = max_date[:10]
                    try:
                        d_obj = _dt.strptime(d_str, "%Y-%m-%d").date()
                        delta = (today - d_obj).days
                        kline_dates[sym] = {"date": d_str, "days_old": delta}
                        if delta > kline_stale_days:
                            stale_symbols.append(f"{sym}({delta}d)")
                    except Exception:
                        pass
                details["kline"] = kline_dates
                if stale_symbols:
                    issues.append(
                        f"kline_data 持仓 K 线陈旧（> {kline_stale_days} 天）："
                        f"{', '.join(stale_symbols)}"
                    )
        except Exception as e:
            logger.warning(f"[Staleness] kline_data 检查失败：{e}")

        # ---- 3. factor_snapshots V1 最新日期 ----
        try:
            today = date.today()
            with self.db._get_conn() as conn:
                row = conn.execute(
                    "SELECT MAX(date) FROM factor_snapshots WHERE version='v1'"
                ).fetchone()
            v1_latest = row[0] if row else None
            if v1_latest:
                d_obj = _dt.strptime(v1_latest[:10], "%Y-%m-%d").date()
                delta = (today - d_obj).days
                details["factor_v1"] = {"date": v1_latest, "days_old": delta}
                if delta > factor_stale_days:
                    issues.append(
                        f"factor_snapshots V1 最新 {v1_latest}，已陈旧 {delta} 天"
                        f"（> {factor_stale_days} 天阈值）"
                    )
            else:
                issues.append("factor_snapshots V1 完全无数据")
                details["factor_v1"] = {"date": None, "days_old": None}
        except Exception as e:
            logger.warning(f"[Staleness] factor_snapshots 检查失败：{e}")

        # ---- 触发告警 ----
        if issues:
            logger.warning(f"[Staleness] ⚠️ 检测到 {len(issues)} 项陈旧问题")
            try:
                body_lines = [
                    f"**时间**：{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"**陈旧项**：{len(issues)}",
                    "",
                ]
                for i, msg in enumerate(issues, 1):
                    body_lines.append(f"{i}. {msg}")
                body_lines.append("")
                body_lines.append("**可能原因**：")
                body_lines.append("- LongPort API 间歇性失败")
                body_lines.append("- daily-scan 没真正跑（launchd 可能挂了）")
                body_lines.append("- _refresh_position_prices 报错被静默")
                body_lines.append("")
                body_lines.append("**排查命令**：`./run_daily.sh` 或查 `~/ai_quant/logs/launchd.out.log`")
                body_text = "\n".join(body_lines)
                self.alerter.warning(
                    body_text,
                    title="⚠️ 数据陈旧度告警",
                    tags=["daily_scan", "staleness"],
                    markdown=body_text,
                )
            except Exception as e:
                logger.warning(f"[Staleness] 告警发送失败：{e}")
        else:
            logger.info("[Staleness] ✅ 数据新鲜度正常")

        return {"issues": issues, "details": details}

    # ============================================================
    # 阶段11 P1-3（dev-13）：手动补跑历史 daily_perf
    # ============================================================

    def replay_dates(
        self,
        from_date: str,
        to_date: Optional[str] = None,
        dry_run: bool = True,
    ) -> dict:
        """
        手动补跑某段历史日期的 daily_performance。

        典型使用场景：
        - 本周 daily-scan 跑了但持仓 current_price 没刷新，
          导致 daily_perf 多天数据相同。修了 P0-1 后用这个方法补跑历史。

        实现策略：
        - 一次拉每只持仓 30 根历史 K 线（覆盖 from_date 到 to_date）
        - 对每个目标日期 d：从 K 线里按 date 找该日 close → update_prices →
          take_daily_snapshot（INSERT OR REPLACE，覆盖旧值）
        - dry_run=True 时只显示 diff 不写库

        Args:
            from_date: 起始日期 YYYY-MM-DD
            to_date: 结束日期 YYYY-MM-DD（默认今天）
            dry_run: True=只显示 diff 不写库

        Returns:
            {"processed": [...], "skipped": [...], "errors": [...]}
        """
        from datetime import date as _date, datetime as _dt

        result = {"processed": [], "skipped": [], "errors": [], "dry_run": dry_run}

        try:
            d_from = _dt.strptime(from_date, "%Y-%m-%d").date()
        except Exception:
            raise ValueError(f"from_date 格式错误（应为 YYYY-MM-DD）：{from_date}")

        if to_date:
            try:
                d_to = _dt.strptime(to_date, "%Y-%m-%d").date()
            except Exception:
                raise ValueError(f"to_date 格式错误：{to_date}")
        else:
            d_to = _date.today()

        if d_to < d_from:
            raise ValueError(f"to_date ({d_to}) 不能早于 from_date ({d_from})")

        positions = list(self.trader.positions.keys())
        if not positions:
            logger.warning("[Replay] 当前无持仓，无法补跑")
            return result

        # 计算 span：from 到 to 之间最多 N 天，再 + 5 缓冲
        span_days = (d_to - d_from).days + 1
        kline_count = max(span_days + 10, 30)

        logger.info(
            f"[Replay] {'[DRY RUN] ' if dry_run else ''}"
            f"补跑 {d_from} ~ {d_to}（{span_days} 天），持仓 {len(positions)} 只，"
            f"拉 {kline_count} 根 K 线"
        )

        # 1. 一次拉所有持仓的 K 线
        try:
            fetched = self.fetcher.fetch_history(
                symbols=positions,
                period="1d",
                count=kline_count,
                save_to_db=not dry_run,  # dry_run 时不写 kline_data
            )
        except Exception as e:
            logger.exception(f"[Replay] 批量拉 K 线失败：{e}")
            result["errors"].append({"phase": "fetch_history", "error": str(e)})
            return result

        # 2. 构造 {symbol: {date_str: close}} 的字典加速查表
        kline_index = {}
        for sym, df in fetched.items():
            if df is None or df.empty:
                continue
            kline_index[sym] = {}
            for _, row in df.iterrows():
                d_str = str(row["date"])[:10]
                try:
                    kline_index[sym][d_str] = float(row["close"])
                except Exception:
                    pass

        # 3. 取所有 trading days（用 calendar 排除周末/假日）
        trading_days = []
        cur = d_from
        while cur <= d_to:
            if self.calendar.is_trading_day(cur):
                trading_days.append(cur)
            cur += timedelta(days=1)

        logger.info(f"[Replay] {len(trading_days)} 个交易日待补跑")

        # 4. 逐日 replay
        for d in trading_days:
            d_str = d.isoformat()
            try:
                # 构造该日价格字典
                prices = {}
                missing = []
                for sym in positions:
                    klines = kline_index.get(sym, {})
                    close = klines.get(d_str)
                    if close is not None and close > 0:
                        prices[sym] = close
                    else:
                        # 该日没有 K 线（可能是节假日，或该 symbol 当天停牌）
                        # 退化策略：用最近一个交易日的 close
                        sorted_dates = sorted([k for k in klines.keys() if k <= d_str])
                        if sorted_dates:
                            prices[sym] = klines[sorted_dates[-1]]
                            missing.append(f"{sym}(用 {sorted_dates[-1]})")
                        else:
                            missing.append(f"{sym}(无可用)")

                if not prices:
                    result["skipped"].append({"date": d_str, "reason": "no_prices"})
                    logger.warning(f"[Replay] {d_str} 无可用价格，跳过")
                    continue

                # 查 daily_perf 旧值
                with self.db._get_conn() as conn:
                    old = conn.execute(
                        "SELECT total_assets, market_value, daily_pnl FROM daily_performance "
                        "WHERE trade_mode='paper' AND date=?",
                        (d_str,),
                    ).fetchone()

                # 模拟 update_prices + 计算新市值（不直接调 update_prices 避免污染当前内存）
                new_market_value = sum(
                    prices.get(sym, 0) * self.trader.positions[sym]["quantity"]
                    for sym in positions
                )
                new_total = self.trader.cash + new_market_value

                old_total = float(old[0]) if old else None
                old_mv = float(old[1]) if old else None
                diff = (new_total - old_total) if old_total is not None else None

                entry = {
                    "date": d_str,
                    "old_total": old_total,
                    "new_total": new_total,
                    "old_mv": old_mv,
                    "new_mv": new_market_value,
                    "diff": diff,
                    "missing": missing,
                }

                if dry_run:
                    logger.info(
                        f"[Replay-DRY] {d_str}: total {old_total} → {new_total:.2f} "
                        f"(diff {diff:+.2f}) mv {old_mv} → {new_market_value:.2f}"
                        + (f" [缺数: {missing}]" if missing else "")
                    )
                else:
                    # 真补：刷价 → take_daily_snapshot（INSERT OR REPLACE）
                    self.trader.update_prices(prices)
                    self.trader.take_daily_snapshot(scan_date=d_str)
                    logger.info(
                        f"[Replay] ✅ {d_str}: total {old_total} → {new_total:.2f} "
                        f"(diff {diff:+.2f})"
                    )

                result["processed"].append(entry)

            except Exception as e:
                logger.exception(f"[Replay] {d_str} 失败：{e}")
                result["errors"].append({"date": d_str, "error": str(e)})

        # 5. 真补完后，把 trader 状态持久化（避免后续 daily-scan 用到旧内存）
        if not dry_run and result["processed"]:
            try:
                self.trader.save_state()
                logger.info("[Replay] ✅ trader 状态已持久化")
            except Exception as e:
                logger.warning(f"[Replay] save_state 失败（忽略）：{e}")

        logger.info(
            f"[Replay] 完成：{len(result['processed'])} 处理 / "
            f"{len(result['skipped'])} 跳过 / {len(result['errors'])} 失败"
        )
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

        # 阶段11 P1-1（dev-13）：数据陈旧度检查（只读，独立于主流程）
        # 这是为了避免再次出现"market_value 8 天不变没人发现"的悲剧
        try:
            self._check_data_staleness()
        except Exception as e:
            logger.warning(f"[DailyScan] 陈旧度检查失败（忽略）：{e}")

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
    # 阶段11 P1-3（dev-13）：手动补跑历史 daily_perf
    parser.add_argument("--replay-date", type=str, default=None,
                        help="补跑历史 daily_perf 起始日 YYYY-MM-DD（与 --replay-to 配合使用）")
    parser.add_argument("--replay-to", type=str, default=None,
                        help="补跑结束日 YYYY-MM-DD（默认今天，与 --replay-date 配合）")
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

    # ===== 阶段11 P1-3（dev-13）：补跑历史模式（独立入口，与 daily-scan 互斥）=====
    if args.replay_date:
        print(f"\n🔁 Replay 模式启动 | 范围: {args.replay_date} ~ "
              f"{args.replay_to or '今天'}")
        print(f"   --dry-run={'是' if args.dry_run else '否（会写库）'}")
        print("-" * 80)
        try:
            replay_result = orch.replay_dates(
                from_date=args.replay_date,
                to_date=args.replay_to,
                dry_run=args.dry_run,
            )
        except ValueError as e:
            print(f"❌ 参数错误：{e}")
            sys.exit(1)

        print(f"\n✅ Replay 完成 | "
              f"处理 {len(replay_result['processed'])} 天 / "
              f"跳过 {len(replay_result['skipped'])} 天 / "
              f"失败 {len(replay_result['errors'])} 天")
        if replay_result['processed']:
            print("\n变化明细（前 10 条）：")
            print(f"{'日期':<12}{'旧 total':>12}{'新 total':>12}{'差额':>10}")
            for entry in replay_result['processed'][:10]:
                old_t = f"{entry['old_total']:.2f}" if entry['old_total'] else "无"
                new_t = f"{entry['new_total']:.2f}"
                diff = f"{entry['diff']:+.2f}" if entry['diff'] else "新增"
                print(f"{entry['date']:<12}{old_t:>12}{new_t:>12}{diff:>10}")
        if args.dry_run:
            print("\n⚠️ 这是 dry-run，未写入数据库。确认无误后去掉 --dry-run 真正补跑。")
        sys.exit(0)

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
        v1_can_run_downstream = False
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
                    body_text = "\n".join(body_lines)
                    orch.alerter.warning(
                        body_text,
                        title=title,
                        tags=["daily_scan", "v1_factor_fail"],
                        markdown=body_text,
                    )
                except Exception as ae:
                    logger.warning(f"[DailyScan] V1 失败告警发送失败（忽略）：{ae}")

            # 阶段11 P1-2（dev-13）：V1 失败时若 DB 有历史 snapshot，仍允许下游周报跑
            # weekly_rotation_report 自带 fallback：找最新可用 V1 日期
            # 这样避免"V1 失败 8 天 → 下游全部停摆"的连锁失败
            # 注：此处不写 fallback 假数据，只允许下游用最新历史 snapshot
            v1_can_run_downstream = v1_ok
            if not v1_ok:
                try:
                    with orch.db._get_conn() as conn:
                        latest = conn.execute(
                            "SELECT MAX(date) FROM factor_snapshots WHERE version='v1'"
                        ).fetchone()[0]
                    if latest:
                        v1_can_run_downstream = True
                        print(f"  ℹ️ V1 失败但有历史 snapshot ({latest}) → 允许下游周报用 fallback")
                except Exception as e:
                    logger.warning(f"[DailyScan] 检查 V1 历史 snapshot 失败：{e}")

            # V1 成功 → 推送排名变化（阶段9 V1 集成 Phase C）
            # 注：FactorNotifier 推"今天 vs 昨天"排名变化，V1 失败时今天没新数据，无法推
            #     所以这里仍用 v1_ok（不能用 fallback）
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
            v1_can_run_downstream = False

        # 阶段10 A3：周度调仓分析报告（仅周一触发，即处理周五数据时）
        # target_date.weekday() == 4 表示处理的是周五数据，意味着这是周一/周二早上跑的
        # 设计：每周 1 次推送，避免每天刷屏
        # 阶段11 P1-2：v1_ok 改为 v1_can_run_downstream（允许 fallback）
        if not args.dry_run and v1_can_run_downstream:
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

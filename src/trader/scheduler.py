"""
Paper Trading Scheduler - 基于 APScheduler 的调度器

Jobs（时间基于美东时间 ET）：
- pre_market (09:25 ET)：数据拉取 + 健康检查
- intraday_open (09:35 ET)：开盘后策略扫描
- intraday_mid (12:30 ET)：盘中监控（止损止盈检查）
- intraday_pre_close (15:45 ET)：收盘前最后一次信号扫描
- post_close (16:05 ET)：每日收盘对账 + 快照 + 告警汇总

交易日判断：USMarketCalendar.is_trading_day()
非交易日跳过所有 Job。
"""

import sys
import os
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytz
from loguru import logger

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.market_calendar import USMarketCalendar
from src.monitor.alerts import get_alerter, AlertLevel


# ET 时区
ET = pytz.timezone("America/New_York")


class PaperTradingScheduler:
    """Paper Trading 调度器。

    由外部 orchestrator（run_paper_trade.py）实例化，注入各阶段的回调：
        pre_market_fn   : 盘前数据拉取 + 健康检查
        scan_fn         : 策略扫描 + 下单（盘中执行 2-3 次）
        monitor_fn      : 止损止盈检查（盘中每 15 分钟可选）
        post_close_fn   : 收盘对账 + 快照
    """

    def __init__(
        self,
        pre_market_fn: Callable,
        scan_fn: Callable,
        monitor_fn: Optional[Callable] = None,
        post_close_fn: Optional[Callable] = None,
        calendar: Optional[USMarketCalendar] = None,
        monitor_interval_minutes: int = 30,
    ):
        if not APSCHEDULER_AVAILABLE:
            raise RuntimeError("APScheduler 未安装：pip install APScheduler")

        self.pre_market_fn = pre_market_fn
        self.scan_fn = scan_fn
        self.monitor_fn = monitor_fn
        self.post_close_fn = post_close_fn
        self.calendar = calendar or USMarketCalendar()
        self.monitor_interval = monitor_interval_minutes

        self.scheduler = BlockingScheduler(timezone=ET)
        self.alerter = get_alerter()
        self._setup_jobs()

    def _setup_jobs(self):
        """注册 5 类 Job。"""
        # 1) 盘前 09:25 ET：数据拉取
        self.scheduler.add_job(
            self._wrap(self.pre_market_fn, "pre_market"),
            CronTrigger(day_of_week="mon-fri", hour=9, minute=25, timezone=ET),
            id="pre_market",
            name="盘前数据拉取",
            max_instances=1,
        )

        # 2) 开盘后 09:35 ET：第一轮信号扫描（等第一根 5min bar 成型后）
        self.scheduler.add_job(
            self._wrap(self.scan_fn, "intraday_open"),
            CronTrigger(day_of_week="mon-fri", hour=9, minute=35, timezone=ET),
            id="intraday_open",
            name="开盘信号扫描",
            max_instances=1,
        )

        # 3) 盘中 15:45 ET：收盘前最后一次扫描（日线策略的关键时点）
        self.scheduler.add_job(
            self._wrap(self.scan_fn, "intraday_pre_close"),
            CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=ET),
            id="intraday_pre_close",
            name="收盘前信号扫描",
            max_instances=1,
        )

        # 4) 盘中每 N 分钟：止损止盈监控（可选）
        if self.monitor_fn is not None:
            self.scheduler.add_job(
                self._wrap(self.monitor_fn, "intraday_monitor"),
                CronTrigger(
                    day_of_week="mon-fri",
                    hour="9-15",
                    minute=f"*/{self.monitor_interval}",
                    timezone=ET,
                ),
                id="intraday_monitor",
                name=f"盘中监控 (每{self.monitor_interval}分钟)",
                max_instances=1,
            )

        # 5) 盘后 16:05 ET：收盘对账
        if self.post_close_fn is not None:
            self.scheduler.add_job(
                self._wrap(self.post_close_fn, "post_close"),
                CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=ET),
                id="post_close",
                name="盘后对账",
                max_instances=1,
            )

    def _wrap(self, fn: Callable, job_name: str):
        """包装 Job：交易日判断 + 异常捕获 + 告警。"""
        def wrapped(*args, **kwargs):
            now_et = datetime.now(ET)
            today = now_et.date()

            if not self.calendar.is_trading_day(today):
                logger.info(f"[{job_name}] 今日非交易日 ({today})，跳过")
                return

            logger.info(f"[{job_name}] ▶️ 开始执行 @ {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            try:
                fn(*args, **kwargs)
                logger.info(f"[{job_name}] ✅ 完成")
            except Exception as e:
                logger.exception(f"[{job_name}] ❌ 执行异常: {e}")
                self.alerter.critical(
                    f"Scheduler job `{job_name}` 异常：{e}",
                    title=f"{job_name} 失败",
                    tags=["scheduler", "error"],
                )

        return wrapped

    def start(self):
        """启动调度器（阻塞式，Ctrl+C 退出）。"""
        # 启动时发一次告警
        self.alerter.info(
            f"Paper Trading Scheduler 已启动\n"
            f"当前 ET: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Jobs: {[j.id for j in self.scheduler.get_jobs()]}\n"
            f"下次运行：\n" + "\n".join(
                f"  {j.id}: {j.next_run_time}" for j in self.scheduler.get_jobs()
            ),
            title="Scheduler 启动",
            tags=["scheduler"],
        )
        logger.info("Scheduler starting... (Press Ctrl+C to exit)")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped by user")
            self.alerter.warning(
                "Paper Trading Scheduler 被手动停止",
                title="Scheduler 关闭",
                tags=["scheduler"],
            )

    def list_jobs(self) -> List[Dict]:
        """返回所有 Job 及下次执行时间（便于 run_paper_trade 启动前展示）。"""
        now = datetime.now(ET)
        out = []
        for j in self.scheduler.get_jobs():
            next_time = getattr(j, "next_run_time", None)
            if next_time is None:
                # Scheduler 未 start 时，从 trigger 计算一次
                try:
                    next_time = j.trigger.get_next_fire_time(None, now)
                except Exception:
                    next_time = None
            out.append({
                "id": j.id,
                "name": j.name,
                "next_run_time": str(next_time) if next_time else "—",
            })
        return out

    def run_once(self, job_id: str):
        """手动触发一次指定 Job（调试用）。"""
        job = self.scheduler.get_job(job_id)
        if job is None:
            logger.error(f"Job {job_id} not found. Available: "
                         f"{[j.id for j in self.scheduler.get_jobs()]}")
            return
        logger.info(f"▶️ Running {job_id} manually")
        job.func()

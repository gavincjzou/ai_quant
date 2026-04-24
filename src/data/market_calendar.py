"""
Market Calendar - 美股市场交易日历
管理交易时间、休市日、时区转换。
"""

from datetime import datetime, date, time, timedelta
from typing import Optional

import pytz
from loguru import logger


# 美股主要休市日（联邦假日）
# 每年需要更新，这里列出固定日期的假日
US_FIXED_HOLIDAYS_2024_2026 = {
    # 2024
    date(2024, 1, 1),   # New Year's Day
    date(2024, 1, 15),  # MLK Day
    date(2024, 2, 19),  # Presidents' Day
    date(2024, 3, 29),  # Good Friday
    date(2024, 5, 27),  # Memorial Day
    date(2024, 6, 19),  # Juneteenth
    date(2024, 7, 4),   # Independence Day
    date(2024, 9, 2),   # Labor Day
    date(2024, 11, 28), # Thanksgiving
    date(2024, 12, 25), # Christmas
    # 2025
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),   # Independence Day observed
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027（阶段7 新增，防 2027 初假日误判）
    date(2027, 1, 1),
    date(2027, 1, 18),  # MLK
    date(2027, 2, 15),  # Presidents'
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial
    date(2027, 6, 18),  # Juneteenth observed (19 is Saturday)
    date(2027, 7, 5),   # Independence Day observed (4 is Sunday)
    date(2027, 9, 6),   # Labor
    date(2027, 11, 25), # Thanksgiving
    date(2027, 12, 24), # Christmas observed (25 is Saturday)
}


class USMarketCalendar:
    """美股市场日历"""

    # 时区
    ET = pytz.timezone("America/New_York")
    UTC = pytz.utc

    # 常规交易时段 (Eastern Time)
    REGULAR_OPEN = time(9, 30)
    REGULAR_CLOSE = time(16, 0)

    # 盘前盘后 (Eastern Time)
    PRE_MARKET_OPEN = time(4, 0)
    PRE_MARKET_CLOSE = time(9, 30)
    AFTER_HOURS_OPEN = time(16, 0)
    AFTER_HOURS_CLOSE = time(20, 0)

    def __init__(self, holidays: Optional[set] = None):
        self.holidays = holidays or US_FIXED_HOLIDAYS_2024_2026

    def is_trading_day(self, d: date) -> bool:
        """
        判断是否为交易日。
        
        排除周末和美股假日。
        """
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        if d in self.holidays:
            return False
        return True

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """
        判断当前时刻是否在常规交易时段内。
        
        Args:
            dt: 要检查的时间（UTC 或带时区），默认为当前时间
        """
        if dt is None:
            dt = datetime.now(self.UTC)

        # 转换为东部时间
        et_dt = dt.astimezone(self.ET)
        
        if not self.is_trading_day(et_dt.date()):
            return False

        current_time = et_dt.time()
        return self.REGULAR_OPEN <= current_time < self.REGULAR_CLOSE

    def is_extended_hours(self, dt: Optional[datetime] = None) -> bool:
        """判断是否在盘前或盘后时段"""
        if dt is None:
            dt = datetime.now(self.UTC)

        et_dt = dt.astimezone(self.ET)
        
        if not self.is_trading_day(et_dt.date()):
            return False

        current_time = et_dt.time()
        in_pre = self.PRE_MARKET_OPEN <= current_time < self.PRE_MARKET_CLOSE
        in_after = self.AFTER_HOURS_OPEN <= current_time < self.AFTER_HOURS_CLOSE
        return in_pre or in_after

    def next_trading_day(self, d: Optional[date] = None) -> date:
        """获取下一个交易日"""
        if d is None:
            d = date.today()
        
        next_d = d + timedelta(days=1)
        while not self.is_trading_day(next_d):
            next_d += timedelta(days=1)
        return next_d

    def prev_trading_day(self, d: Optional[date] = None) -> date:
        """获取上一个交易日"""
        if d is None:
            d = date.today()
        
        prev_d = d - timedelta(days=1)
        while not self.is_trading_day(prev_d):
            prev_d -= timedelta(days=1)
        return prev_d

    def get_trading_days(self, start: date, end: date) -> list:
        """获取指定日期范围内的所有交易日"""
        days = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    def trading_days_between(self, start: date, end: date) -> list:
        """等价于 get_trading_days（阶段7 新增语义化别名，闭区间）"""
        return self.get_trading_days(start, end)

    def last_closed_trading_day(
        self,
        now_utc: Optional[datetime] = None,
        close_buffer_minutes: int = 30,
    ) -> date:
        """
        返回"截至当前时刻已经完整收盘"的最近一个交易日（美东）。

        严格模式：只要美东还未过 16:00 + buffer（默认 16:30），当日就不算已收盘，
        返回前一个交易日。用于日线扫描补跑逻辑，避免用半日数据生成信号。

        Args:
            now_utc: 当前 UTC 时间，None 时用 datetime.now(UTC)
            close_buffer_minutes: 收盘后缓冲分钟数（给数据源留时间）

        Returns:
            已完整收盘的最近交易日（date）
        """
        if now_utc is None:
            now_utc = datetime.now(self.UTC)

        et_now = now_utc.astimezone(self.ET)
        today = et_now.date()

        # 如果今天是交易日且已过收盘+缓冲，则今天就算已收盘
        if self.is_trading_day(today):
            close_time = self.ET.localize(
                datetime.combine(today, self.REGULAR_CLOSE)
            )
            ready_time = close_time + timedelta(minutes=close_buffer_minutes)
            if et_now >= ready_time:
                return today

        # 否则找前一个交易日
        return self.prev_trading_day(today)

    def time_to_market_open(self, dt: Optional[datetime] = None) -> Optional[timedelta]:
        """
        计算距离下次开盘的时间。
        
        Returns:
            timedelta or None (如果当前就在交易时段内)
        """
        if dt is None:
            dt = datetime.now(self.UTC)

        if self.is_market_open(dt):
            return None  # 已经在交易时段

        et_dt = dt.astimezone(self.ET)
        
        # 如果今天是交易日且还没到开盘
        if self.is_trading_day(et_dt.date()):
            open_dt = self.ET.localize(
                datetime.combine(et_dt.date(), self.REGULAR_OPEN)
            )
            if et_dt < open_dt:
                return open_dt - et_dt

        # 否则找下一个交易日
        next_day = self.next_trading_day(et_dt.date())
        next_open = self.ET.localize(
            datetime.combine(next_day, self.REGULAR_OPEN)
        )
        return next_open - et_dt

    @staticmethod
    def to_et(dt: datetime) -> datetime:
        """将任意时间转换为美东时间"""
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(USMarketCalendar.ET)

    @staticmethod
    def to_utc(dt: datetime) -> datetime:
        """将任意时间转换为 UTC"""
        if dt.tzinfo is None:
            dt = USMarketCalendar.ET.localize(dt)
        return dt.astimezone(pytz.utc)

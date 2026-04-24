"""
Time Utilities - 时间工具
UTC 与市场时区互转、交易时段判断等通用时间函数。
"""

from datetime import datetime, date, timedelta
from typing import Optional

import pytz

# 常用时区
ET = pytz.timezone("America/New_York")
UTC = pytz.utc
CST = pytz.timezone("Asia/Shanghai")


def now_et() -> datetime:
    """获取当前美东时间"""
    return datetime.now(ET)


def now_utc() -> datetime:
    """获取当前 UTC 时间"""
    return datetime.now(UTC)


def to_et(dt: datetime) -> datetime:
    """转换为美东时间"""
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return dt.astimezone(ET)


def to_utc(dt: datetime) -> datetime:
    """转换为 UTC"""
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt.astimezone(UTC)


def to_cst(dt: datetime) -> datetime:
    """转换为中国时间（北京时间）"""
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return dt.astimezone(CST)


def format_date(dt, fmt: str = "%Y-%m-%d") -> str:
    """格式化日期"""
    if isinstance(dt, datetime):
        return dt.strftime(fmt)
    if isinstance(dt, date):
        return dt.strftime(fmt)
    return str(dt)


def parse_date(s: str, fmt: str = "%Y-%m-%d") -> date:
    """解析日期字符串"""
    return datetime.strptime(s, fmt).date()


def trading_days_between(start: date, end: date) -> int:
    """计算两个日期之间的交易日数量（近似值，不考虑假日）"""
    total_days = (end - start).days
    weeks = total_days // 7
    remainder = total_days % 7
    trading = weeks * 5 + min(remainder, 5)
    return max(0, trading)


def is_same_trading_day(dt1: datetime, dt2: datetime) -> bool:
    """判断两个时间是否属于同一交易日"""
    et1 = to_et(dt1)
    et2 = to_et(dt2)
    return et1.date() == et2.date()

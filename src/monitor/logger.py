"""
Logger - 日志系统配置
基于 loguru 配置分级日志，按日期轮转。
"""

import os
import sys

from loguru import logger


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    rotation: str = "1 day",
    retention: str = "30 days",
):
    """
    配置日志系统。
    
    日志文件:
    - system.log: 系统运行日志（INFO+）
    - trade.log: 交易专用日志（所有交易操作）
    - error.log: 错误日志（WARNING+）
    """
    os.makedirs(log_dir, exist_ok=True)

    # 移除默认 handler
    logger.remove()

    # 控制台输出（简洁格式）
    logger.add(
        sys.stdout,
        level=log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 系统日志
    logger.add(
        os.path.join(log_dir, "system_{time:YYYY-MM-DD}.log"),
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    # 交易日志
    logger.add(
        os.path.join(log_dir, "trade_{time:YYYY-MM-DD}.log"),
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        filter=lambda record: "trade" in record["extra"].get("tags", [])
        or "order" in record["message"].lower()
        or "signal" in record["message"].lower()
        or "executed" in record["message"].lower(),
    )

    # 错误日志
    logger.add(
        os.path.join(log_dir, "error_{time:YYYY-MM-DD}.log"),
        level="WARNING",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

    logger.info(f"Logging initialized: dir={log_dir}, level={log_level}")

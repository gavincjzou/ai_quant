"""
Trading State - 交易系统状态持久化

阶段7 新增：Paper Trading 的"日线扫描模式"需要跨进程记忆
- last_scan_date: 最后一次成功完成扫描的美东交易日
- 用于 `run_paper_trade.py --daily-scan` 检测交易日 gap 并幂等补跑

设计：
- 使用 SQLite KV 表（与现有 DatabaseManager 共用 data_cache/quant.db）
- 表名 `trading_state`，结构 (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)
- 只存字符串 value，调用方自行 JSON 编解码
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Optional

from loguru import logger


# 约定的 key 常量
KEY_LAST_SCAN_DATE = "paper.last_scan_date"          # ISO date string, 美东
KEY_LAST_RUN_AT = "paper.last_run_at"                # ISO datetime
KEY_LAST_DAILY_SCAN_SUMMARY = "paper.last_daily_scan_summary"  # JSON
KEY_LAST_WECOM_TEST_AT = "alerts.last_wecom_test_at"  # ISO datetime


class TradingState:
    """通用 KV 状态存储（SQLite 单表）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_table()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_table(self):
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    # ----------------- 通用 KV -----------------

    def get(self, key: str, default: Any = None) -> Any:
        """读取 key 对应的 JSON 值；不存在时返回 default。"""
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT value FROM trading_state WHERE key = ?", (key,)
                )
                row = cur.fetchone()
                if row is None:
                    return default
                return json.loads(row["value"])
        except Exception as e:
            logger.warning(f"[TradingState] get {key} failed: {e}")
            return default

    def set(self, key: str, value: Any) -> bool:
        """写入 key 的 JSON 值（覆盖）。"""
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO trading_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, payload, datetime.now().isoformat(timespec="seconds")),
                )
            return True
        except Exception as e:
            logger.warning(f"[TradingState] set {key} failed: {e}")
            return False

    def delete(self, key: str) -> bool:
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM trading_state WHERE key = ?", (key,))
            return True
        except Exception as e:
            logger.warning(f"[TradingState] delete {key} failed: {e}")
            return False

    # ----------------- 业务封装 -----------------

    def get_last_scan_date(self) -> Optional[date]:
        """返回最后一次成功扫描的美东交易日。"""
        v = self.get(KEY_LAST_SCAN_DATE)
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except Exception:
            return None

    def set_last_scan_date(self, d: date) -> bool:
        return self.set(KEY_LAST_SCAN_DATE, d.isoformat())

    def touch_last_run(self) -> bool:
        return self.set(KEY_LAST_RUN_AT, datetime.now().isoformat(timespec="seconds"))

    def save_scan_summary(self, summary: dict) -> bool:
        return self.set(KEY_LAST_DAILY_SCAN_SUMMARY, summary)

    def get_scan_summary(self) -> Optional[dict]:
        return self.get(KEY_LAST_DAILY_SCAN_SUMMARY)

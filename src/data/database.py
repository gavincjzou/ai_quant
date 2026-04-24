"""
Database Manager - SQLite 数据库管理
管理行情数据缓存、交易记录、回测结果的本地存储。
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

import pandas as pd
from loguru import logger


class DatabaseManager:
    """SQLite 数据库管理器"""

    def __init__(self, db_path: str = "data_cache/quant.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_tables()

    @contextmanager
    def _get_conn(self):
        """获取数据库连接的上下文管理器"""
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

    def _init_tables(self):
        """初始化数据库表结构"""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # 行情K线数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS kline_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL DEFAULT '1d',
                    date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    turnover REAL DEFAULT 0,
                    adjust_type TEXT DEFAULT 'qfq',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, period, date, adjust_type)
                )
            """)

            # 交易记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    trade_mode TEXT NOT NULL DEFAULT 'paper',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    commission REAL DEFAULT 0,
                    slippage REAL DEFAULT 0,
                    strategy_name TEXT,
                    signal_reason TEXT,
                    executed_at TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 持仓快照表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS position_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_mode TEXT NOT NULL DEFAULT 'paper',
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    market_price REAL,
                    unrealized_pnl REAL,
                    snapshot_at TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 回测结果表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    final_value REAL NOT NULL,
                    total_return REAL,
                    annual_return REAL,
                    max_drawdown REAL,
                    sharpe_ratio REAL,
                    win_rate REAL,
                    profit_loss_ratio REAL,
                    trade_count INTEGER,
                    avg_holding_days REAL,
                    params_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 每日绩效表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_mode TEXT NOT NULL DEFAULT 'paper',
                    date TEXT NOT NULL,
                    total_assets REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    daily_pnl REAL DEFAULT 0,
                    daily_return REAL DEFAULT 0,
                    cumulative_return REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0,
                    trade_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(trade_mode, date)
                )
            """)

            # 创建索引
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_kline_symbol_date "
                "ON kline_data(symbol, period, date)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_symbol "
                "ON trade_records(symbol, executed_at)"
            )

        logger.info(f"Database initialized: {self.db_path}")

    # ----------------------------------------------------------
    # K-line Data CRUD
    # ----------------------------------------------------------

    def save_kline(
        self,
        symbol: str,
        df: pd.DataFrame,
        period: str = "1d",
        adjust_type: str = "qfq",
    ):
        """
        保存K线数据（upsert）。
        
        Args:
            symbol: 标的代码
            df: DataFrame with columns [date, open, high, low, close, volume, turnover]
            period: K线周期
            adjust_type: 复权方式
        """
        if df.empty:
            return

        with self._get_conn() as conn:
            for _, row in df.iterrows():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO kline_data 
                    (symbol, period, date, open, high, low, close, volume, turnover, adjust_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        period,
                        str(row["date"]),
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        int(row["volume"]),
                        row.get("turnover", 0),
                        adjust_type,
                    ),
                )
        logger.debug(f"Saved {len(df)} kline bars for {symbol} ({period})")

    def load_kline(
        self,
        symbol: str,
        period: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust_type: str = "qfq",
    ) -> pd.DataFrame:
        """
        加载K线数据。
        
        Returns:
            DataFrame with OHLCV columns, sorted by date ascending
        """
        query = (
            "SELECT date, open, high, low, close, volume, turnover "
            "FROM kline_data WHERE symbol = ? AND period = ? AND adjust_type = ?"
        )
        params: list = [symbol, period, adjust_type]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY date ASC"

        with self._get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    # ----------------------------------------------------------
    # Trade Records
    # ----------------------------------------------------------

    def save_trade(self, trade: dict):
        """保存一条交易记录"""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO trade_records 
                (order_id, trade_mode, symbol, side, quantity, price,
                 commission, slippage, strategy_name, signal_reason, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.get("order_id"),
                    trade.get("trade_mode", "paper"),
                    trade["symbol"],
                    trade["side"],
                    trade["quantity"],
                    trade["price"],
                    trade.get("commission", 0),
                    trade.get("slippage", 0),
                    trade.get("strategy_name"),
                    trade.get("signal_reason"),
                    trade.get("executed_at", datetime.now().isoformat()),
                ),
            )

    def load_trades(
        self,
        trade_mode: str = "paper",
        symbol: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """加载交易记录"""
        query = "SELECT * FROM trade_records WHERE trade_mode = ?"
        params: list = [trade_mode]

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if start_date:
            query += " AND executed_at >= ?"
            params.append(start_date)
        if end_date:
            query += " AND executed_at <= ?"
            params.append(end_date)

        query += " ORDER BY executed_at ASC"

        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)

    # ----------------------------------------------------------
    # Backtest Results
    # ----------------------------------------------------------

    def save_backtest_result(self, result: dict):
        """保存回测结果"""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO backtest_results 
                (strategy_name, symbol, start_date, end_date, initial_capital,
                 final_value, total_return, annual_return, max_drawdown,
                 sharpe_ratio, win_rate, profit_loss_ratio, trade_count,
                 avg_holding_days, params_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["strategy_name"],
                    result["symbol"],
                    result["start_date"],
                    result["end_date"],
                    result["initial_capital"],
                    result["final_value"],
                    result.get("total_return"),
                    result.get("annual_return"),
                    result.get("max_drawdown"),
                    result.get("sharpe_ratio"),
                    result.get("win_rate"),
                    result.get("profit_loss_ratio"),
                    result.get("trade_count"),
                    result.get("avg_holding_days"),
                    result.get("params_json"),
                ),
            )

    def load_backtest_results(
        self, strategy_name: Optional[str] = None
    ) -> pd.DataFrame:
        """加载回测结果"""
        query = "SELECT * FROM backtest_results"
        params = []
        if strategy_name:
            query += " WHERE strategy_name = ?"
            params.append(strategy_name)
        query += " ORDER BY created_at DESC"

        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)

    # ----------------------------------------------------------
    # Daily Performance
    # ----------------------------------------------------------

    def save_daily_performance(self, perf: dict):
        """保存每日绩效快照"""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_performance 
                (trade_mode, date, total_assets, cash, market_value,
                 daily_pnl, daily_return, cumulative_return, max_drawdown, trade_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    perf.get("trade_mode", "paper"),
                    perf["date"],
                    perf["total_assets"],
                    perf["cash"],
                    perf["market_value"],
                    perf.get("daily_pnl", 0),
                    perf.get("daily_return", 0),
                    perf.get("cumulative_return", 0),
                    perf.get("max_drawdown", 0),
                    perf.get("trade_count", 0),
                ),
            )

"""
YFinance Client - yfinance 适配器
作为历史回测的主数据源（LongPort API 配额有限，且无法方便拉取超 1000 根K线）。
接口向 LongPortClient 对齐，DataFetcher 可透明切换。

限流处理：
- 使用 Ticker.history() 而非 yf.download()（前者限流更宽松）
- 失败后指数退避 + 重试
- 批量请求之间强制 sleep 间隔
"""

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

try:
    import yfinance as yf

    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    logger.warning("yfinance not installed. Run: pip install yfinance")

# curl_cffi 可选：伪装成 Chrome 绕过 Yahoo Finance 的限流
try:
    from curl_cffi import requests as cffi_requests

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


def _build_session():
    """构造带浏览器指纹的 requests session，绕过 Yahoo 限流。"""
    if CURL_CFFI_AVAILABLE:
        return cffi_requests.Session(impersonate="chrome")
    return None


# LongPort -> yfinance symbol 映射
def _to_yf_symbol(symbol: str) -> str:
    """把 LongPort 风格 symbol 转成 yfinance 风格。"""
    if symbol.endswith(".US"):
        return symbol[:-3]  # AAPL.US -> AAPL
    return symbol


# yfinance period 映射
_YF_INTERVAL_MAP = {
    "1d": "1d",
    "1w": "1wk",
    "1M": "1mo",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
}


class YFinanceClient:
    """yfinance 历史数据客户端。"""

    def __init__(self, config: Optional[dict] = None):
        if not YFINANCE_AVAILABLE:
            raise RuntimeError("yfinance not installed. Run: pip install yfinance")
        self._config = config or {}
        self._retry_max = self._config.get("retry_max", 4)
        self._retry_base_delay = self._config.get("retry_base_delay", 2.0)
        self._request_interval = self._config.get("request_interval", 1.0)
        self._last_request_time = 0.0
        # 带浏览器指纹的 session，绕过 Yahoo 限流
        self._session = _build_session()
        if self._session is None:
            logger.warning(
                "curl_cffi not installed - yfinance 限流风险较高，"
                "建议 pip install curl_cffi"
            )
        else:
            logger.info("yfinance using curl_cffi Chrome-impersonating session")

    def _throttle(self):
        """强制请求间隔，避免触发Yahoo限流。"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._request_interval:
            time.sleep(self._request_interval - elapsed)
        self._last_request_time = time.time()

    def _fetch_with_retry(
        self,
        yf_symbol: str,
        start: str,
        end: str,
        interval: str,
        auto_adjust: bool,
    ) -> pd.DataFrame:
        """Ticker.history() + 指数退避重试。"""
        last_err = None
        for attempt in range(1, self._retry_max + 1):
            self._throttle()
            try:
                ticker = yf.Ticker(yf_symbol, session=self._session)
                df = ticker.history(
                    start=start,
                    end=end,
                    interval=interval,
                    auto_adjust=auto_adjust,
                    actions=False,
                    timeout=30,
                )
                if df is not None and not df.empty:
                    return df
                last_err = "empty response"
            except Exception as e:
                last_err = str(e)

            if attempt < self._retry_max:
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"yfinance {yf_symbol} attempt {attempt}/{self._retry_max} "
                    f"failed ({last_err}), retry in {delay}s"
                )
                time.sleep(delay)

        logger.error(
            f"yfinance {yf_symbol} failed after {self._retry_max} attempts: {last_err}"
        )
        return pd.DataFrame()

    def get_history_kline(
        self,
        symbol: str,
        period: str = "1d",
        count: int = 1000,
        adjust: str = "qfq",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """拉取历史K线。"""
        yf_symbol = _to_yf_symbol(symbol)
        interval = _YF_INTERVAL_MAP.get(period, "1d")

        if adjust == "hfq":
            raise ValueError(
                "hfq (后复权) 不支持。回测应使用 qfq (前复权) 保证价格与实盘一致。"
            )
        auto_adjust = adjust == "qfq"

        # 构造 start/end
        if start_date is None or end_date is None:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=int(count * 1.5))
            start_date = start_date or start_dt.strftime("%Y-%m-%d")
            end_date = end_date or end_dt.strftime("%Y-%m-%d")

        df = self._fetch_with_retry(
            yf_symbol, start_date, end_date, interval, auto_adjust
        )

        if df is None or df.empty:
            logger.warning(f"yfinance returned empty DataFrame for {symbol}")
            return pd.DataFrame()

        # MultiIndex columns 打平
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 列名标准化
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
                "Adj Close": "adj_close",
            }
        )

        # 重置索引
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else "Datetime"
        df = df.rename(columns={date_col: "date"})

        # 去时区
        if pd.api.types.is_datetime64tz_dtype(df["date"]):
            df["date"] = df["date"].dt.tz_convert("America/New_York").dt.tz_localize(None)
        df["date"] = pd.to_datetime(df["date"])

        # 补 turnover
        if "turnover" not in df.columns:
            df["turnover"] = df["close"] * df["volume"]

        cols = ["date", "open", "high", "low", "close", "volume", "turnover"]
        df = df[cols].sort_values("date").reset_index(drop=True)

        # count 兜底：如果结果超 count，截最后 count 根
        if len(df) > count:
            df = df.tail(count).reset_index(drop=True)

        logger.debug(
            f"yfinance {symbol}({yf_symbol}): {len(df)} bars, "
            f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}"
        )
        return df

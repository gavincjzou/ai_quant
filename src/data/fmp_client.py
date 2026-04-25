"""FMP (Financial Modeling Prep) API Client - 阶段 9 V1 新增

职责：
1. 从 FMP stable endpoint 拉取 Quality 三指标（净利率、毛利率、营收增速）
2. 拉取 sector/industry 元数据（用于 Industry 因子加权）
3. 日限额跟踪（免费档 250 次/天）
4. SQLite 缓存（fundamental_ratios 表，避免重复调用）
5. 失败降级（超限/网络错误时返回空 dict，不中断主流程）

重要：FMP 2025 改版后新注册用户只能用 stable endpoints，v3 是 Legacy。
文档：https://site.financialmodelingprep.com/developer/docs/stable
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests
import yaml
from loguru import logger


class FMPClient:
    """Financial Modeling Prep API 客户端（stable endpoint）"""

    DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"
    DEFAULT_TIMEOUT = 10
    DEFAULT_DAILY_LIMIT = 250
    QUOTA_WARNING_THRESHOLD = 200

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        db=None,
        config_path: Optional[str] = None,
    ):
        """
        初始化。凭证加载顺序：
        1. 显式传入的 api_key（最高优先级）
        2. 环境变量 FMP_API_KEY
        3. config/monitor.local.yaml 的 fmp.api_key
        """
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.db = db
        self._ready = False

        if not self.api_key:
            cfg = self._load_local_yaml(config_path)
            if cfg and cfg.get("fmp"):
                self.api_key = cfg["fmp"].get("api_key")
                self.base_url = cfg["fmp"].get("base_url", self.base_url)

        if self.api_key:
            self._ready = True
            logger.info(f"[FMPClient] ready (base={self.base_url})")
        else:
            logger.warning(
                "[FMPClient] 未找到 API key（env FMP_API_KEY 或 monitor.local.yaml 的 fmp.api_key）"
                "，所有调用将返回空数据（降级模式）"
            )

        # 本次会话调用计数（简化版）
        self._call_count_today = 0

    @property
    def ready(self) -> bool:
        return self._ready

    @staticmethod
    def _load_local_yaml(config_path: Optional[str] = None) -> Optional[dict]:
        """读 config/monitor.local.yaml"""
        if config_path is None:
            # 从当前模块往上找 ai_quant 项目根
            here = os.path.dirname(os.path.abspath(__file__))
            proj_root = os.path.abspath(os.path.join(here, "..", ".."))
            config_path = os.path.join(proj_root, "config", "monitor.local.yaml")
        if not os.path.isfile(config_path):
            return None
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"[FMPClient] 读 {config_path} 失败：{e}")
            return None

    # ----------------------------------------------------------
    # 低阶调用
    # ----------------------------------------------------------

    def _get(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        max_retries: int = 2,
    ) -> Optional[list]:
        """
        GET 一次 FMP endpoint。失败/超限返回 None。

        Args:
            endpoint: "/profile" or "/ratios-ttm" 等
            params: 额外 query params（不含 apikey，函数内自动拼）
            max_retries: 网络错误时的重试次数
        """
        if not self._ready:
            return None

        url = self.base_url + endpoint
        full_params = {"apikey": self.api_key, **(params or {})}

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = requests.get(url, params=full_params, timeout=self.DEFAULT_TIMEOUT)
                self._call_count_today += 1

                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and data.get("Error Message"):
                        logger.warning(f"[FMP] {endpoint} error: {data.get('Error Message')}")
                        return None
                    return data if isinstance(data, list) else [data]

                if r.status_code == 403:
                    logger.error(
                        f"[FMP] {endpoint} 403 Forbidden—可能超免费额度或凭证无效。"
                        f"已调用 {self._call_count_today} 次。body={r.text[:200]}"
                    )
                    self._ready = False  # 禁用后续调用
                    return None

                if r.status_code == 429:
                    # Rate limited，等一下再试
                    logger.warning(f"[FMP] {endpoint} 429，等 2s 重试 ({attempt+1}/{max_retries})")
                    time.sleep(2)
                    continue

                logger.warning(f"[FMP] {endpoint} HTTP {r.status_code}: {r.text[:200]}")
                last_err = f"HTTP {r.status_code}"

            except requests.RequestException as e:
                last_err = str(e)
                logger.warning(f"[FMP] {endpoint} 网络错误 ({attempt+1}): {e}")
                time.sleep(1.5 ** attempt)

        logger.error(f"[FMP] {endpoint} 重试 {max_retries} 次失败: {last_err}")
        return None

    # ----------------------------------------------------------
    # 高阶业务接口
    # ----------------------------------------------------------

    def fetch_profile(self, symbol: str) -> Dict[str, Optional[object]]:
        """返回 {sector, industry, market_cap, beta, company_name}"""
        data = self._get("/profile", {"symbol": symbol})
        if not data:
            return {"symbol": symbol, "sector": None, "industry": None,
                    "market_cap": None, "beta": None, "company_name": None}
        d = data[0]
        return {
            "symbol": symbol,
            "sector": d.get("sector"),
            "industry": d.get("industry"),
            "market_cap": d.get("marketCap"),
            "beta": d.get("beta"),
            "company_name": d.get("companyName"),
        }

    def fetch_ratios_ttm(self, symbol: str) -> Dict[str, Optional[float]]:
        """返回盈利质量三指标（+ 辅助）"""
        data = self._get("/ratios-ttm", {"symbol": symbol})
        if not data:
            return {"symbol": symbol, "net_margin": None, "gross_margin": None,
                    "operating_margin": None, "debt_to_equity": None}
        d = data[0]
        return {
            "symbol": symbol,
            "net_margin": d.get("netProfitMarginTTM"),
            "gross_margin": d.get("grossProfitMarginTTM"),
            "operating_margin": d.get("operatingProfitMarginTTM"),
            "debt_to_equity": d.get("debtToEquityRatioTTM"),
        }

    def fetch_financial_growth(self, symbol: str) -> Dict[str, Optional[float]]:
        """返回成长指标 {revenue_growth, net_income_growth, eps_growth}"""
        data = self._get("/financial-growth", {"symbol": symbol, "limit": 1})
        if not data:
            return {"symbol": symbol, "revenue_growth": None,
                    "net_income_growth": None, "eps_growth": None}
        d = data[0]
        return {
            "symbol": symbol,
            "revenue_growth": d.get("revenueGrowth"),
            "net_income_growth": d.get("netIncomeGrowth"),
            "eps_growth": d.get("epsgrowth"),
        }

    def fetch_fundamentals_one(self, symbol: str) -> Dict[str, Optional[object]]:
        """
        单只标的拉齐所有字段（profile + ratios + growth 合并）。
        消耗 3 次 API 调用。
        """
        bare = symbol.split(".")[0] if "." in symbol else symbol  # AAPL.US → AAPL
        profile = self.fetch_profile(bare)
        ratios = self.fetch_ratios_ttm(bare)
        growth = self.fetch_financial_growth(bare)

        merged = {"symbol": symbol}  # 用完整 symbol（含 .US）作为主键
        merged.update({k: v for k, v in profile.items() if k != "symbol"})
        merged.update({k: v for k, v in ratios.items() if k != "symbol"})
        merged.update({k: v for k, v in growth.items() if k != "symbol"})
        merged["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        return merged

    def batch_fetch(
        self,
        symbols: List[str],
        sleep_between: float = 0.15,
        use_cache: bool = True,
        cache_ttl_hours: int = 168,  # 缓存一周
    ) -> pd.DataFrame:
        """
        批量拉取多只标的的基本面数据。

        Args:
            symbols: 标的列表（带 .US 后缀）
            sleep_between: 每次调用之间的间隔（避免 rate limit）
            use_cache: 是否优先用 fundamental_ratios 缓存表
            cache_ttl_hours: 缓存新鲜度（默认 7 天内算新鲜）

        Returns:
            DataFrame，columns: symbol, sector, industry, market_cap, beta,
                               net_margin, gross_margin, operating_margin,
                               revenue_growth, net_income_growth, fetched_at
        """
        rows = []
        hit_cache = 0
        api_calls = 0

        for sym in symbols:
            cached = None
            if use_cache and self.db is not None:
                cached = self._load_cache(sym, cache_ttl_hours)

            if cached is not None:
                rows.append(cached)
                hit_cache += 1
                continue

            if not self._ready:
                # API 不可用但又没缓存：放一个空行保证 symbol 不丢
                logger.warning(f"[FMP] {sym} 无缓存且 API 不可用，placeholder 空数据")
                rows.append({"symbol": sym})
                continue

            row = self.fetch_fundamentals_one(sym)
            rows.append(row)
            api_calls += 1

            # 写缓存
            if self.db is not None:
                self._save_cache(row)

            # 限额预警
            if self._call_count_today > self.QUOTA_WARNING_THRESHOLD:
                logger.warning(
                    f"[FMP] 今日已调用 {self._call_count_today} 次，"
                    f"接近 250 次限额！"
                )

            if sleep_between > 0:
                time.sleep(sleep_between)

        logger.info(
            f"[FMP] batch_fetch: {len(symbols)} 只 = {hit_cache} 缓存命中 + "
            f"{api_calls * 3} 次 API 调用（每只 profile+ratios+growth 3 次）"
        )

        df = pd.DataFrame(rows)
        return df

    # ----------------------------------------------------------
    # SQLite 缓存（fundamental_ratios 表）
    # ----------------------------------------------------------

    def _load_cache(self, symbol: str, ttl_hours: int) -> Optional[dict]:
        if self.db is None:
            return None
        try:
            with self.db._get_conn() as conn:
                cur = conn.execute(
                    "SELECT * FROM fundamental_ratios WHERE symbol = ?",
                    (symbol,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            d = dict(row) if hasattr(row, "keys") else {
                k: row[i] for i, k in enumerate(
                    ["symbol", "sector", "industry", "market_cap", "beta",
                     "company_name", "net_margin", "gross_margin",
                     "operating_margin", "debt_to_equity", "revenue_growth",
                     "net_income_growth", "eps_growth", "fetched_at"]
                )
            }
            # 判断 TTL
            fetched_at = d.get("fetched_at")
            if fetched_at:
                try:
                    dt = datetime.fromisoformat(fetched_at)
                    age_h = (datetime.now() - dt).total_seconds() / 3600
                    if age_h > ttl_hours:
                        return None  # 过期
                except Exception:
                    pass
            return d
        except Exception as e:
            logger.debug(f"[FMP] _load_cache({symbol}) 失败: {e}")
            return None

    def _save_cache(self, row: dict):
        if self.db is None:
            return
        try:
            with self.db._get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO fundamental_ratios (
                        symbol, sector, industry, market_cap, beta, company_name,
                        net_margin, gross_margin, operating_margin, debt_to_equity,
                        revenue_growth, net_income_growth, eps_growth, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("symbol"),
                        row.get("sector"),
                        row.get("industry"),
                        row.get("market_cap"),
                        row.get("beta"),
                        row.get("company_name"),
                        row.get("net_margin"),
                        row.get("gross_margin"),
                        row.get("operating_margin"),
                        row.get("debt_to_equity"),
                        row.get("revenue_growth"),
                        row.get("net_income_growth"),
                        row.get("eps_growth"),
                        row.get("fetched_at"),
                    ),
                )
        except Exception as e:
            logger.debug(f"[FMP] _save_cache 失败: {e}")

    def get_daily_quota_used(self) -> int:
        return self._call_count_today

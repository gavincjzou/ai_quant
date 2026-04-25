"""WestockClient - 腾讯自选股 westock-data 数据源封装（阶段 9 V1）

替代 FMPClient 作为 Quality 因子数据源。理由：
1. FMP 免费档 Quality endpoints 覆盖率仅 59%（半导体核心 MU/MRVL/QCOM 全 402）
2. yfinance 白天也被 IP rate limited
3. westock-data（腾讯自选股）100% 覆盖 + 真 ROE + 无限调用

职责：
1. 调用 npx westock-data-clawhub@1.0.4 CLI，解析 Markdown 表格输出
2. Symbol 格式转换：LongPort "NVDA.US" → Westock "usNVDA.OQ"（NASDAQ/NYSE/AMEX）
3. 提供 fetch_fundamentals / fetch_profile / batch_fetch API
4. SQLite 缓存（fundamental_ratios 表，7 天 TTL）
5. 失败降级（单只失败返回空 dict，不中断主流程）

命令文档：~/.workbuddy/skills/westock-data/references/ai_usage_guide.md
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger


# npx 路径（WorkBuddy managed Node）
_NODE_BIN = os.path.expanduser("~/.workbuddy/binaries/node/versions/22.12.0/bin")
_NPX = os.path.join(_NODE_BIN, "npx")


class WestockClient:
    """腾讯自选股 westock-data CLI 客户端"""

    PACKAGE = "westock-data-clawhub@1.0.4"
    DEFAULT_TIMEOUT = 30

    def __init__(self, db=None):
        self.db = db
        # Symbol 映射缓存：LongPort symbol → Westock symbol
        # 避免每次重查 search
        self._symbol_map: Dict[str, str] = {}
        self._ready = self._check_npx()

    @property
    def ready(self) -> bool:
        return self._ready

    @staticmethod
    def _check_npx() -> bool:
        if not os.path.isfile(_NPX):
            logger.warning(f"[Westock] npx 不存在: {_NPX}")
            return False
        return True

    # ----------------------------------------------------------
    # 低阶：CLI 调用 + MD 表格解析
    # ----------------------------------------------------------

    def _run_cli(
        self,
        command: str,
        *args: str,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """运行 westock-data CLI，返回 stdout。失败返回 None。"""
        if not self._ready:
            return None
        cmd = [_NPX, "-y", self.PACKAGE, command, *args]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.DEFAULT_TIMEOUT,
                env={**os.environ, "PATH": f"{_NODE_BIN}:{os.environ.get('PATH','')}"},
            )
            if r.returncode != 0:
                logger.warning(
                    f"[Westock] CLI 非 0 返回 ({r.returncode}): {command} {args[:2]} "
                    f"stderr: {r.stderr[:200]}"
                )
                # 有些命令返回 1 但仍有输出（如 search），尝试解析 stdout
                if r.stdout:
                    return r.stdout
                return None
            return r.stdout
        except subprocess.TimeoutExpired:
            logger.warning(f"[Westock] CLI 超时: {command} {args[:2]}")
            return None
        except Exception as e:
            logger.warning(f"[Westock] CLI 异常: {e}")
            return None

    @staticmethod
    def _parse_md_table(section: str) -> Dict[str, str]:
        """解析 MD 表格的第一个数据行，返回 {header: value}"""
        lines = [
            ln.strip()
            for ln in section.split("\n")
            if ln.strip().startswith("|")
        ]
        if len(lines) < 3:
            return {}
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        # lines[1] 是分隔行 | --- | ---，跳过
        values = [v.strip() for v in lines[2].strip("|").split("|")]
        if len(headers) != len(values):
            return {}
        return dict(zip(headers, values))

    @classmethod
    def _extract_section(cls, output: str, section_name: str) -> Optional[str]:
        """从 CLI 输出中提取指定 section（如 income/balance/cashflow）"""
        # section 格式 **income** ... **balance**
        pattern = rf"\*\*{section_name}\*\*(.*?)(\*\*\w+\*\*|$)"
        m = re.search(pattern, output, re.DOTALL)
        if not m:
            return None
        return m.group(1).strip()

    @staticmethod
    def _to_float(v: Optional[str]) -> Optional[float]:
        if v is None or v == "" or v == "null":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------
    # Symbol 映射：LongPort (AAPL.US) → Westock (usAAPL.OQ)
    # ----------------------------------------------------------

    # 预设硬编码映射（减少 search 调用，覆盖 watchlist + V1 扩展）
    HARDCODED_MAP: Dict[str, str] = {
        # 科技 NASDAQ
        "AAPL.US": "usAAPL.OQ", "MSFT.US": "usMSFT.OQ",
        "GOOGL.US": "usGOOGL.OQ", "META.US": "usMETA.OQ",
        "AMZN.US": "usAMZN.OQ", "NVDA.US": "usNVDA.OQ",
        "AMD.US": "usAMD.OQ", "NFLX.US": "usNFLX.OQ",
        "TSLA.US": "usTSLA.OQ",
        # 半导体 NASDAQ（V1 新增）
        "AVGO.US": "usAVGO.OQ", "MU.US": "usMU.OQ",
        "MRVL.US": "usMRVL.OQ", "QCOM.US": "usQCOM.OQ",
        "INTC.US": "usINTC.OQ", "SMCI.US": "usSMCI.OQ",
        # 软件/数据 NYSE/NASDAQ
        "PLTR.US": "usPLTR.OQ",  # 已迁 NASDAQ
        # 台积电 ADR
        "TSM.US": "usTSM.N",
        # 金融 NYSE
        "JPM.US": "usJPM.N", "BAC.US": "usBAC.N",
        "V.US": "usV.N", "MA.US": "usMA.N",
        # 医药
        "LLY.US": "usLLY.N", "JNJ.US": "usJNJ.N",
        # 消费 NYSE
        "WMT.US": "usWMT.N", "KO.US": "usKO.N",
        "MCD.US": "usMCD.N",
        # 能源工业 NYSE
        "XOM.US": "usXOM.N", "CAT.US": "usCAT.N",
        # 中概 NYSE/NASDAQ
        "BABA.US": "usBABA.N", "PDD.US": "usPDD.OQ",
        # Oracle
        "ORCL.US": "usORCL.N",
        # ETF
        "SPY.US": "usSPY.AM", "QQQ.US": "usQQQ.OQ",
        "IWM.US": "usIWM.AM",
    }

    def longport_to_westock(self, symbol: str) -> Optional[str]:
        """LongPort symbol → Westock symbol，带缓存"""
        if symbol in self._symbol_map:
            return self._symbol_map[symbol]
        if symbol in self.HARDCODED_MAP:
            self._symbol_map[symbol] = self.HARDCODED_MAP[symbol]
            return self._symbol_map[symbol]

        # 没硬编码，动态 search
        bare = symbol.split(".")[0] if "." in symbol else symbol
        out = self._run_cli("search", bare)
        if out:
            # 匹配第一行 "| usXXX.YY | ..."
            m = re.search(rf"\|\s*(us{re.escape(bare)}\.[A-Z]+)\s*\|", out)
            if m:
                westock_sym = m.group(1)
                self._symbol_map[symbol] = westock_sym
                logger.debug(f"[Westock] 动态映射 {symbol} → {westock_sym}")
                return westock_sym

        logger.warning(f"[Westock] 无法映射 symbol: {symbol}")
        return None

    # ----------------------------------------------------------
    # 业务接口：profile / finance
    # ----------------------------------------------------------

    def fetch_profile(self, symbol: str) -> Dict[str, Optional[object]]:
        """返回 {sector, industry, company_name, exchange}"""
        ws = self.longport_to_westock(symbol)
        if not ws:
            return {"symbol": symbol}

        out = self._run_cli("profile", ws)
        if not out:
            return {"symbol": symbol}

        # profile 输出是单个 MD 表，无 **section** 标记
        # 直接按 | 开头行解析
        data = self._parse_md_table(out)
        return {
            "symbol": symbol,
            "sector": data.get("sector"),
            "industry": data.get("industry"),
            "company_name": data.get("name"),
            "exchange": data.get("exchange"),
        }

    def fetch_financials(self, symbol: str) -> Dict[str, Optional[float]]:
        """
        返回盈利质量 + 成长指标：
          net_margin, gross_margin, operating_margin, roe, roa, net_income,
          revenue, financial_year
        """
        ws = self.longport_to_westock(symbol)
        if not ws:
            return {"symbol": symbol}

        out = self._run_cli("finance", ws, "--num", "1")
        if not out:
            return {"symbol": symbol}

        income_section = self._extract_section(out, "income")
        balance_section = self._extract_section(out, "balance")

        income = self._parse_md_table(income_section) if income_section else {}
        balance = self._parse_md_table(balance_section) if balance_section else {}

        return {
            "symbol": symbol,
            # income
            "net_margin": self._to_float(income.get("NetMargin")),
            "gross_margin": self._to_float(income.get("GrossMargin")),
            "operating_margin": self._to_float(income.get("OperatingMargin")),
            "net_income": self._to_float(income.get("NetIncome")),
            "revenue": self._to_float(income.get("Sales")),
            "financial_year": income.get("FinancialYear"),
            # balance
            "roe": self._to_float(balance.get("ROE")),
            "roa": self._to_float(balance.get("ROA")),
            "debt_to_equity": None,  # westock 不直接给，可用 LiabilityToAsset 近似
            "liability_to_asset": self._to_float(balance.get("LiabilityToAsset")),
        }

    def fetch_fundamentals_one(self, symbol: str) -> Dict[str, Optional[object]]:
        """profile + financials 合并（一个标的一次性拉齐）"""
        profile = self.fetch_profile(symbol)
        fin = self.fetch_financials(symbol)
        merged = {"symbol": symbol}
        for k, v in profile.items():
            if k != "symbol":
                merged[k] = v
        for k, v in fin.items():
            if k != "symbol":
                merged[k] = v
        merged["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        return merged

    def batch_fetch(
        self,
        symbols: List[str],
        sleep_between: float = 0.2,
        use_cache: bool = True,
        cache_ttl_hours: int = 168,
    ) -> pd.DataFrame:
        """
        批量拉多只标的。

        westock CLI 本来支持逗号分隔批量，但 finance 命令批量时某些标的失败会让
        整个命令 exit code 非 0，影响稳定性。故这里采用串行 + 缓存。
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

            row = self.fetch_fundamentals_one(sym)
            rows.append(row)
            api_calls += 1

            if self.db is not None:
                self._save_cache(row)

            if sleep_between > 0:
                time.sleep(sleep_between)

        logger.info(
            f"[Westock] batch_fetch: {len(symbols)} 只 = {hit_cache} 缓存 + "
            f"{api_calls} 次 CLI 调用（每次 2 个 subprocess = profile + finance）"
        )
        return pd.DataFrame(rows)

    # ----------------------------------------------------------
    # SQLite 缓存（复用 fundamental_ratios 表）
    # ----------------------------------------------------------

    CACHE_COLUMNS = [
        "symbol", "sector", "industry", "company_name",
        "net_margin", "gross_margin", "operating_margin",
        "roe", "revenue_growth", "fetched_at",
    ]

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
            # sqlite3.Row 可按列名访问
            d = {k: row[k] for k in row.keys()}
            fetched_at = d.get("fetched_at")
            if fetched_at:
                try:
                    dt = datetime.fromisoformat(fetched_at)
                    age_h = (datetime.now() - dt).total_seconds() / 3600
                    if age_h > ttl_hours:
                        return None
                except Exception:
                    pass
            return d
        except Exception as e:
            logger.debug(f"[Westock] load_cache({symbol}) 失败: {e}")
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
                        None,  # market_cap westock 不直接给（用 LongPort 的 total_market_value）
                        None,  # beta 先不用
                        row.get("company_name"),
                        row.get("net_margin"),
                        row.get("gross_margin"),
                        row.get("operating_margin"),
                        row.get("liability_to_asset"),  # 近似 debt_to_equity
                        None,  # revenue_growth 需要多期对比，未来 V1.5 加
                        None, None,
                        row.get("fetched_at"),
                    ),
                )
        except Exception as e:
            logger.debug(f"[Westock] save_cache 失败: {e}")

"""FactorNotifier - 阶段 9 V1 生产化集成 Phase C

职责：V1 因子跑完后自动对比前后两份 snapshot，推送 Top-10 + 排名变化到企业微信。

核心设计：
1. 按 DISTINCT date DESC 取前 2 个日期（不是前 2 条记录）
   - 因为同一天可能有多次 save（午后手动 + 晚间自动），按记录取会永远只看到同一天
2. 首次跑（只有 1 个日期）→ 推 Top-10 基线，提示"从下次开始追踪变化"
3. 常规跑（≥2 个日期）→ 推 Top-5 + 新进/掉出/±5 名变化
4. 失败降级：AlertManager 未配置 webhook 时优雅退出，log warning
5. 消息长度控制在 1500 字以内（企微 4096 字上限）
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from loguru import logger

from src.data.database import DatabaseManager
from src.monitor.alerts import AlertLevel, AlertManager


class FactorNotifier:
    """V1 因子排名变化推送器"""

    TOP_N_HEADLINE = 10    # 基线推送多少只
    TOP_N_DIGEST = 5       # 常规推送 Top-N
    RANK_DELTA_THRESHOLD = 5  # 单标的排名变动 ≥N 名才告警

    def __init__(self, db: DatabaseManager, alerter: AlertManager):
        self.db = db
        self.alerter = alerter

    # ------------------------------------------------------------
    # 数据读取
    # ------------------------------------------------------------

    def load_latest_two_v1_dates(self) -> Tuple[Optional[str], Optional[str]]:
        """返回 (current_date, prev_date)。按 DISTINCT date 取。"""
        try:
            with self.db._get_conn() as conn:
                cur = conn.execute(
                    """SELECT DISTINCT date FROM factor_snapshots
                       WHERE version='v1' ORDER BY date DESC LIMIT 2"""
                )
                dates = [r[0] for r in cur.fetchall()]
            if not dates:
                return None, None
            cur_date = dates[0]
            prev_date = dates[1] if len(dates) >= 2 else None
            return cur_date, prev_date
        except Exception as e:
            logger.warning(f"[FactorNotifier] load_dates 失败: {e}")
            return None, None

    def load_ranks_on_date(self, date: str) -> List[dict]:
        """读某日期的全量 V1 排名（含所有字段）"""
        try:
            with self.db._get_conn() as conn:
                cur = conn.execute(
                    """SELECT symbol, rank, total_score,
                              quality_score, industry_score, momentum_score,
                              sector, industry
                       FROM factor_snapshots
                       WHERE version='v1' AND date=?
                       ORDER BY rank ASC""",
                    (date,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"[FactorNotifier] load_ranks_on_date({date}) 失败: {e}")
            return []

    # ------------------------------------------------------------
    # Diff 计算
    # ------------------------------------------------------------

    def compute_diff(self, cur_date: str, prev_date: Optional[str]) -> dict:
        """
        返回：
        {
            "is_baseline": bool,       # 是否首次基线（无 prev）
            "cur_date": str,
            "prev_date": str|None,
            "cur_top10": [{...}],      # 当前 Top-10 全量
            "cur_top5": [{...}],       # 当前 Top-5
            "new_in_top10": [str],     # 新进 Top-10 的 symbol
            "dropped_out": [str],      # 从 Top-10 掉出的 symbol
            "rank_shifts": [           # 单标的排名变动 ≥5 名
                {"symbol": "NVDA.US", "prev_rank": 20, "cur_rank": 8, "delta": +12}
            ],
        }
        """
        cur_rows = self.load_ranks_on_date(cur_date)

        result = {
            "is_baseline": prev_date is None,
            "cur_date": cur_date,
            "prev_date": prev_date,
            "cur_top10": cur_rows[: self.TOP_N_HEADLINE],
            "cur_top5": cur_rows[: self.TOP_N_DIGEST],
            "new_in_top10": [],
            "dropped_out": [],
            "rank_shifts": [],
        }

        if prev_date is None:
            return result

        prev_rows = self.load_ranks_on_date(prev_date)
        prev_rank_map = {r["symbol"]: r["rank"] for r in prev_rows}

        cur_top10_syms = {r["symbol"] for r in cur_rows[: self.TOP_N_HEADLINE]}
        prev_top10_syms = {
            r["symbol"] for r in prev_rows if r["rank"] <= self.TOP_N_HEADLINE
        }

        result["new_in_top10"] = sorted(cur_top10_syms - prev_top10_syms)
        result["dropped_out"] = sorted(prev_top10_syms - cur_top10_syms)

        # 单标的排名变化 ≥N 名
        for cur_r in cur_rows:
            sym = cur_r["symbol"]
            prev_r_rank = prev_rank_map.get(sym)
            if prev_r_rank is None:
                continue
            delta = prev_r_rank - cur_r["rank"]  # 正 = 上升
            if abs(delta) >= self.RANK_DELTA_THRESHOLD:
                result["rank_shifts"].append({
                    "symbol": sym,
                    "prev_rank": prev_r_rank,
                    "cur_rank": cur_r["rank"],
                    "delta": delta,
                    "total_score": cur_r.get("total_score", 0),
                })
        # 按 |delta| 降序
        result["rank_shifts"].sort(key=lambda x: abs(x["delta"]), reverse=True)
        # 最多展示 8 个（避免消息过长）
        result["rank_shifts"] = result["rank_shifts"][:8]

        return result

    # ------------------------------------------------------------
    # Markdown 渲染
    # ------------------------------------------------------------

    def render_markdown(self, diff: dict) -> Tuple[str, str]:
        """
        返回 (title, markdown_body)

        企微 Markdown 支持：## 标题、**粗体**、> 引用、表格、`代码`。
        不支持大多数 HTML。
        """
        cur_date = diff["cur_date"]

        if diff["is_baseline"]:
            # 首次基线
            title = f"V1 因子首次基线 | {cur_date}"
            lines = [
                f"# 🎯 V1 多因子首次基线",
                f"",
                f"> 日期：**{cur_date}** · 首次建立 V1 快照",
                f"> 从下次 daily-scan 开始追踪排名变化",
                f"",
                f"## 🏆 当前 Top-10",
                f"",
            ]
            for r in diff["cur_top10"]:
                industry = r.get("industry") or "-"
                lines.append(
                    f"- **#{r['rank']} {r['symbol']}** "
                    f"(score {r.get('total_score', 0):+.2f}) "
                    f"· {industry}"
                )
            return title, "\n".join(lines)

        # 常规推送
        prev_date = diff["prev_date"]
        title = f"V1 因子排名变化 | {cur_date}"
        lines = [
            f"# 🎯 V1 多因子排名变化",
            f"",
            f"> 当前：**{cur_date}** vs 上期：{prev_date}",
            f"",
            f"## 🏆 今日 Top-{self.TOP_N_DIGEST}",
            f"",
        ]
        for r in diff["cur_top5"]:
            industry = r.get("industry") or "-"
            quality = r.get("quality_score") or 0
            industry_s = r.get("industry_score") or 0
            momentum = r.get("momentum_score") or 0
            lines.append(
                f"- **#{r['rank']} {r['symbol']}** "
                f"(score {r.get('total_score', 0):+.2f}) · "
                f"Q {quality:+.1f} / Ind {industry_s:+.1f} / Mom {momentum:+.1f}"
            )
        lines.append("")

        # 新进 Top-10
        if diff["new_in_top10"]:
            lines.append(f"## ✨ 新进 Top-{self.TOP_N_HEADLINE}")
            lines.append("")
            for sym in diff["new_in_top10"]:
                lines.append(f"- **{sym}**")
            lines.append("")

        # 掉出 Top-10
        if diff["dropped_out"]:
            lines.append(f"## 📉 掉出 Top-{self.TOP_N_HEADLINE}")
            lines.append("")
            for sym in diff["dropped_out"]:
                lines.append(f"- {sym}")
            lines.append("")

        # 排名变化 ≥5 名
        if diff["rank_shifts"]:
            lines.append(f"## 🔀 单标的排名变动 ≥{self.RANK_DELTA_THRESHOLD} 名")
            lines.append("")
            for s in diff["rank_shifts"]:
                arrow = "↑" if s["delta"] > 0 else "↓"
                lines.append(
                    f"- **{s['symbol']}** #{s['prev_rank']} → #{s['cur_rank']} "
                    f"({arrow}{abs(s['delta'])})"
                )
            lines.append("")

        if not (diff["new_in_top10"] or diff["dropped_out"] or diff["rank_shifts"]):
            lines.append("> 📊 排名无显著变化")
            lines.append("")

        return title, "\n".join(lines)

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------

    def notify(self) -> bool:
        """
        主入口：读 DB → 算 diff → 渲染 → 推送。

        Returns:
            True = 成功推送（或已调用 send）
            False = 无数据或 alerter 未就绪
        """
        cur_date, prev_date = self.load_latest_two_v1_dates()
        if cur_date is None:
            logger.info("[FactorNotifier] DB 无 V1 快照，skip 推送")
            return False

        diff = self.compute_diff(cur_date, prev_date)
        title, markdown = self.render_markdown(diff)

        # 短文本 fallback（非 WeCom 通道）
        cur_top_syms = [r["symbol"] for r in diff["cur_top5"]]
        summary = f"V1 Top-5 ({cur_date}): {', '.join(cur_top_syms)}"
        if not diff["is_baseline"]:
            if diff["new_in_top10"]:
                summary += f" | 新进 Top-10: {', '.join(diff['new_in_top10'])}"
            if diff["dropped_out"]:
                summary += f" | 掉出 Top-10: {', '.join(diff['dropped_out'])}"

        # 字数控制
        if len(markdown) > 3500:
            logger.warning(f"[FactorNotifier] Markdown 长度 {len(markdown)} 超限，截断")
            markdown = markdown[:3500] + "\n\n> ⚠️ 消息过长已截断"

        logger.info(
            f"[FactorNotifier] 推送 V1 因子 {cur_date}"
            + (" (baseline)" if diff["is_baseline"] else "")
        )

        try:
            self.alerter.send(
                message=summary,
                level=AlertLevel.INFO,
                title=title,
                tags=["v1_factor"],
                markdown=markdown,
            )
            return True
        except Exception as e:
            logger.warning(f"[FactorNotifier] AlertManager.send 失败: {e}")
            return False

"""
weekly_rotation_report.py - 周度持仓轮换分析报告

用途：
    每周（建议周一收市后跑）对比当前持仓 vs V1 Top-5 过去 30 天表现，
    生成 Markdown 周报并推送企业微信。

非目的：
    本脚本不会自动调仓。决策权完全在 Gavin。

使用：
    # 手动跑
    python scripts/weekly_rotation_report.py
    python scripts/weekly_rotation_report.py --top 5 --date 2026-04-25
    python scripts/weekly_rotation_report.py --no-push  # 只生成报告不推企微

    # 自动触发（由 daily-scan 在每周一调用）
    详见 scripts/run_paper_trade.py daily-scan 末尾的 hook
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from src.data.database import DatabaseManager
from src.data.trading_state import TradingState


# ============================================================
# 主流程
# ============================================================

def run_backtest_vs_holdings(
    snapshot_date: str,
    top_n: int = 5,
    days: int = 30,
) -> tuple[bool, str]:
    """调 backtest_factor_screen.py --mode vs_holdings。

    返回 (success, report_path)。
    """
    backtest_script = os.path.join(_PROJECT_ROOT, "scripts", "backtest_factor_screen.py")
    cmd = [
        sys.executable,
        backtest_script,
        "--mode", "vs_holdings",
        "--top", str(top_n),
        "--days", str(days),
        "--date", snapshot_date,
    ]
    logger.info(f"[Weekly] 跑 backtest: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=_PROJECT_ROOT, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"[Weekly] backtest 失败: {result.stderr[-300:]}")
            return False, ""

        # 解析 stdout 找出 报告路径行
        report_path = ""
        for line in result.stdout.split("\n"):
            if "报告路径:" in line:
                # 形如 "  报告路径:                /path/to/vs_holdings_*.md"
                m = re.search(r"报告路径:\s*(\S+\.md)", line)
                if m:
                    report_path = m.group(1).strip()
                    break

        if not report_path:
            # fallback：约定路径
            report_path = os.path.join(
                _PROJECT_ROOT, "output", f"vs_holdings_{snapshot_date}.md"
            )

        if not os.path.exists(report_path):
            logger.error(f"[Weekly] 报告文件不存在: {report_path}")
            return False, ""

        logger.info(f"[Weekly] 报告生成: {report_path}")
        return True, report_path

    except subprocess.TimeoutExpired:
        logger.error("[Weekly] backtest 超时（120s）")
        return False, ""
    except Exception as e:
        logger.error(f"[Weekly] backtest 异常: {e}")
        return False, ""


def build_wecom_summary(report_path: str, snapshot_date: str) -> str:
    """从完整 Markdown 报告抽出适合企微推送的摘要（≤ 1500 字）。

    策略：取核心结论 + 双组指标对比 + 持仓差异分析 + 调仓建议（前几节）
    """
    if not os.path.exists(report_path):
        return f"⚠️ 报告文件不存在: {report_path}"

    content = Path(report_path).read_text(encoding="utf-8")

    # 找标题 + 截到 "## 📦 当前持仓权重明细" 之前
    cutoff = content.find("## 📦 当前持仓权重明细")
    if cutoff > 0:
        summary = content[:cutoff].strip()
    else:
        summary = content[:2000]

    # 长度兜底
    if len(summary) > 3500:
        summary = summary[:3500] + "\n\n_..._（详见完整报告）"

    # 在末尾补完整报告路径
    summary += f"\n\n---\n📂 完整报告：`{os.path.basename(report_path)}`"
    return summary


def push_wecom(markdown_text: str, snapshot_date: str) -> bool:
    """推 markdown 到企业微信。失败不抛异常。"""
    try:
        from src.monitor.alerts import get_alerter
        alerter = get_alerter()

        if not getattr(alerter, "_wecom_enabled", False):
            logger.info("[Weekly] WeCom 未配置，跳过推送（仅生成报告）")
            return False

        title = f"V1 周度调仓分析 · {snapshot_date}"
        # text 摘要给非 markdown 通道用
        text_brief = f"{title}\n（详见 markdown 内容）"

        alerter.info(
            text_brief,
            title=title,
            tags=["weekly_rotation", "factor"],
            markdown=markdown_text,
        )
        logger.info("[Weekly] WeCom 推送完成")
        return True
    except Exception as e:
        logger.warning(f"[Weekly] WeCom 推送失败（忽略）: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="周度持仓轮换分析（V1 Top-N vs 当前持仓 30 天对比）",
    )
    parser.add_argument("--date", type=str, default=None,
                        help="V1 快照日期，默认今天")
    parser.add_argument("--top", type=int, default=5, help="V1 Top-N，默认 5")
    parser.add_argument("--days", type=int, default=30, help="回看天数，默认 30")
    parser.add_argument("--no-push", action="store_true",
                        help="只生成报告，不推企微")
    args = parser.parse_args()

    snapshot_date = args.date or datetime.now().strftime("%Y-%m-%d")

    # 1. 验证 V1 快照存在
    db = DatabaseManager(os.path.join(_PROJECT_ROOT, "data_cache", "quant.db"))
    with db._get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM factor_snapshots WHERE version='v1' AND date=?",
            (snapshot_date,),
        ).fetchone()[0]
    if cnt == 0:
        # 兜底：找最新可用 V1 日期
        with db._get_conn() as conn:
            latest = conn.execute(
                "SELECT MAX(date) FROM factor_snapshots WHERE version='v1'"
            ).fetchone()[0]
        if not latest:
            logger.error("[Weekly] DB 里完全没有 V1 快照，无法生成周报")
            logger.error("提示：先跑 `python scripts/run_factor_screen.py --version v1`")
            return 1
        logger.info(f"[Weekly] {snapshot_date} 无 V1 快照，回退到最新日期 {latest}")
        snapshot_date = latest

    # 2. 验证当前有持仓
    state = TradingState(db.db_path)
    positions = state.get("paper.positions") or {}
    if not positions:
        logger.warning("[Weekly] 当前无持仓数据，跳过周报")
        return 1

    logger.info(
        f"[Weekly] 准备生成周报：snapshot={snapshot_date}, top={args.top}, "
        f"持仓={len(positions)} 只"
    )

    # 3. 跑 vs_holdings 回测
    ok, report_path = run_backtest_vs_holdings(
        snapshot_date, top_n=args.top, days=args.days,
    )
    if not ok:
        logger.error("[Weekly] backtest 失败，跳过推送")
        return 1

    # 4. 推企微（除非 --no-push）
    if not args.no_push:
        summary_md = build_wecom_summary(report_path, snapshot_date)
        push_wecom(summary_md, snapshot_date)

    print("\n" + "=" * 72)
    print(f"✅ 周度调仓分析完成 · {snapshot_date}")
    print(f"   报告：{report_path}")
    print(f"   推送：{'已推送' if not args.no_push else '已跳过（--no-push）'}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

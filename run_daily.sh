#!/bin/bash
# ============================================================
# AI Quant - 日常 Paper Trading 启动脚本（方案 C：日线扫描模式）
# ------------------------------------------------------------
# 用法：
#   ./run_daily.sh              # 正常跑
#   ./run_daily.sh --dry-run    # 只看 gap 不执行
#
# 调用时机：
#   - 每次开电脑后手动执行一次
#   - 或由 launchd / WorkBuddy 自动化任务触发
# ============================================================

set -e

# 项目根目录（脚本所在目录）
cd "$(dirname "$0")"

# 使用项目 venv
PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "❌ 未找到 venv，请先激活项目环境：.venv/bin/activate"
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  🚀 AI Quant - Daily Scan 启动（$(date '+%Y-%m-%d %H:%M:%S'))"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

"$PYTHON" scripts/run_paper_trade.py --daily-scan "$@"

echo ""
echo "✅ 完成。对账报告：output/reconciliation/"
echo "   告警日志：output/alerts.log"

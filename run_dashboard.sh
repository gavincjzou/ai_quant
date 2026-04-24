#!/bin/bash
# ============================================================
# AI Quant Dashboard 快捷脚本
# ============================================================
# 用法：
#   ./run_dashboard.sh          # 生成 HTML + 自动打开浏览器
#   ./run_dashboard.sh --no-open # 只生成不打开
# ============================================================

set -e
cd "$(dirname "$0")"

# 自动检测 Python
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  📊 AI Quant Dashboard 生成中... ($(date '+%Y-%m-%d %H:%M:%S'))"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

if [ "$1" = "--no-open" ]; then
    $PYTHON scripts/build_dashboard.py
else
    $PYTHON scripts/build_dashboard.py --open
fi

echo ""
echo "✅ 完成。文件位置：output/dashboard.html"
echo ""

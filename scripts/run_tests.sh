#!/usr/bin/env bash
# scripts/run_tests.sh - 一键跑全套测试
#
# 使用：
#   ./scripts/run_tests.sh              # 跑所有测试（unittest 风格）
#   ./scripts/run_tests.sh --pytest     # 用 pytest 跑（更友好的输出）
#   ./scripts/run_tests.sh --coverage   # 跑 + 输出覆盖率（需 pip install coverage）
#   ./scripts/run_tests.sh tests/test_factor.py  # 只跑某个文件

set -e

cd "$(dirname "$0")/.."  # 切到项目根目录

PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

MODE="unittest"
TARGET=""

for arg in "$@"; do
    case "$arg" in
        --pytest)
            MODE="pytest"
            ;;
        --coverage)
            MODE="coverage"
            ;;
        tests/*)
            TARGET="$arg"
            ;;
        *)
            ;;
    esac
done

echo "============================================================"
echo "🧪 AI Quant 测试套件"
echo "  Python: $PYTHON"
echo "  Mode:   $MODE"
echo "  Target: ${TARGET:-tests/}"
echo "============================================================"
echo ""

case "$MODE" in
    pytest)
        $PYTHON -m pytest "${TARGET:-tests/}" -v --tb=short
        ;;
    coverage)
        $PYTHON -m coverage run --source=src,scripts -m unittest discover -v "${TARGET:-tests}" 2>&1
        echo ""
        echo "============================================================"
        echo "📊 覆盖率报告"
        echo "============================================================"
        $PYTHON -m coverage report --skip-empty
        $PYTHON -m coverage html -d output/coverage_html
        echo ""
        echo "✅ HTML 报告：output/coverage_html/index.html"
        ;;
    *)
        if [ -n "$TARGET" ]; then
            $PYTHON -m unittest "$TARGET" -v
        else
            $PYTHON -m unittest discover -s tests -v
        fi
        ;;
esac

echo ""
echo "✅ 测试结束"

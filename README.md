# 🤖 AI Quant Trading System

基于 Python + LongPort OpenAPI 的美股量化交易系统。

## 📋 功能概览

| 模块 | 说明 |
|------|------|
| **数据层** | 长桥 API 行情/K线 + WorkBuddy Skills 财报/宏观数据 |
| **策略层** | 可插拔策略框架（均线交叉/RSI/动量），YAML 配置参数 |
| **回测层** | 基于 backtrader，前复权、佣金滑点、禁止未来函数 |
| **风控层** | 仓位控制、止损止盈、日亏损限额、回撤熔断 |
| **交易层** | 模拟交易 (PaperTrader) + 实盘交易 (LiveTrader) |
| **监控层** | 日志系统、绩效报告、持仓仪表盘 |

## 🚀 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API 密钥
cp .env.example .env
# 编辑 .env 填入你的长桥 API 密钥

# 4. 下载历史数据
python scripts/fetch_data.py --symbols AAPL,MSFT,GOOGL --days 365

# 5. 运行回测
python scripts/run_backtest.py --strategy ma_cross --symbol AAPL

# 6. 启动模拟交易
python scripts/run_paper_trade.py

# 7. 启动实盘交易 (确认风控参数后)
python scripts/run_live_trade.py
```

## 📂 项目结构

```
ai_quant/
├── config/          # YAML 配置文件
├── src/             # 核心代码
│   ├── data/        # 数据采集层
│   ├── strategy/    # 策略引擎层
│   ├── backtest/    # 回测引擎层
│   ├── risk/        # 风控管理层
│   ├── trader/      # 交易执行层
│   ├── monitor/     # 监控报告层
│   └── utils/       # 工具模块
├── scripts/         # 运行脚本
├── tests/           # 单元测试
├── docs/            # Obsidian 知识库
└── logs/            # 运行日志
```

## ⚠️ 风险提示

量化交易存在市场风险，本系统仅供学习研究，不构成投资建议。建议先使用模拟交易充分验证策略后，再以小金额进入实盘。

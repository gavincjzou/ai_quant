<div align="center">

# 🤖 AI Quant Trading System

**基于 Python + LongPort OpenAPI 的美股量化交易系统**

[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Backtrader](https://img.shields.io/badge/backtrader-1.9-green.svg)](https://www.backtrader.com/)
[![License](https://img.shields.io/badge/license-Private-red.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-8-brightgreen.svg)](docs/obsidian-vault/dev-系统开发/)

从策略设计到实盘跑单的完整量化交易工作流 · 支持 Paper Trading · 日线扫描模式 · 企业微信告警 · Cyberpunk Dashboard

</div>

---

## 📖 目录

- [项目简介](#-项目简介)
- [系统架构](#-系统架构)
- [核心特性](#-核心特性)
- [快速开始](#-快速开始)
- [项目结构](#-项目结构)
- [开发里程碑](#-开发里程碑)
- [开发约定](#-开发约定)
- [常用命令](#-常用命令)
- [风险提示](#-风险提示)

---

## 🎯 项目简介

### 解决什么问题

这是一个**个人量化交易研发平台**，目标是：

- 🧪 **系统化研究**：从历史回测到实盘运行的完整工作流，避免"拍脑袋选股"
- 📊 **风险可控**：每笔交易必经 ATR 止损 + 442 分批止盈 + 熔断机制
- 🤖 **全自动化**：launchd 守护进程 + 企业微信告警，无人值守
- 📚 **知识沉淀**：Obsidian vault 把学习笔记 + 开发记录 + 回测结论沉淀成可检索图谱

### 目标用户

- 有一定编程基础的量化交易学习者
- 追求系统化、数据驱动的投资者
- 不满足于"跟单""荐股"的独立研究者

### 当前状态

- ✅ Paper Trading 正式运行（每日 21:30 launchd 自动触发）
- ✅ 28 只标的 watchlist + per_symbol 最优策略映射
- ✅ 企业微信实时告警（成交 / 风控 / 异常）
- ✅ Cyberpunk 风格 HTML Dashboard
- 🚧 阶段 9：多因子选股（规划中，paper 跑满 2 周后启动）
- ⏸️ 实盘接入（Paper 稳定运行 1 个月后评估）

---

## 🏗 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                      数据源                               │
│        LongPort API  |  YFinance  |  SQLite 缓存         │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                    策略引擎层                             │
│  MA Cross  |  RSI  |  Momentum  +  per_symbol 映射       │
│        StrategyManager（择时/信号生成）                    │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                    风控管理层                             │
│  PositionSizer（仓位计算）+ StopLossManager（ATR 442）   │
│          + RiskManager（熔断 / 日损限额）                 │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                    交易执行层                             │
│    PaperTrader（模拟）        →     LiveTrader（实盘）   │
│              OrderManager  |  跨进程状态持久化            │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                    监控报告层                             │
│  企业微信告警 | DailyReconciliation | HTML Dashboard     │
└──────────────────────────────────────────────────────────┘
```

### 模块职责

| 层级 | 模块 | 职责 | 关键文件 |
|------|------|------|---------|
| **数据层** | `src/data/` | 行情拉取 / K 线缓存 / 交易日历 / 状态持久化 | `longport_client.py` `database.py` |
| **策略层** | `src/strategy/` | 信号生成 / 多策略编排 / per_symbol 映射 | `strategy_manager.py` `ma_cross_strategy.py` |
| **回测层** | `src/backtest/` | backtrader 集成 / 指标计算 / 绩效评估 | `engine.py` |
| **风控层** | `src/risk/` | 仓位控制 / ATR 止损 / 熔断保护 | `stop_loss.py` `risk_manager.py` |
| **交易层** | `src/trader/` | 订单管理 / 模拟成交 / 实盘对接 | `paper_trader.py` `order_manager.py` |
| **监控层** | `src/monitor/` | 告警通道 / 对账报告 / 可视化 | `alerts.py` `wecom_channel.py` |

---

## ✨ 核心特性

### 🎯 策略层

- **三大基础策略**：MA Cross（趋势）/ RSI（反转）/ Momentum（动量）
- **per_symbol 映射**：基于 96 次回测的 Calmar Ratio，给每只标的分配最优策略
  - RSI 最佳：LLY, V, COST, JPM, SPY, AAPL...
  - MA 最佳：AMD, GS, WMT, AMZN, JNJ...
  - Momentum 最佳：TSM, NVDA, NFLX, MSFT, META...

### 🛡 风控层

- **ATR 442 分批止盈**：TP1 (1.5R) 平 40% + TP2 (3R) 平 40% + TP3 (4.5R) 平 20%
- **动态止损**：2.5 × ATR 初始止损，TP1 触发后移到保本
- **熔断保护**：单日 -3% / 最大回撤 20% 触发熔断
- **禁止加仓**：同标的已持仓时默认拒绝新 BUY 信号

### 🤖 自动化

- **launchd 守护**：工作日 21:30 北京时间自动跑，登录即跑
- **幂等补跑**：最多自动补 14 个交易日，可连续出差不影响
- **跨进程状态持久化**：cash / positions / stop_loss / risk_state 全部 SQLite 存储

### 📊 监控

- **企业微信实时告警**：买卖成交、风控触发、异常警报
- **每日对账**：`output/reconciliation/YYYY-MM-DD.md`
- **Cyberpunk Dashboard**：深色霓虹主题，ApexCharts 动画，涨红跌绿

---

## 🚀 快速开始

### 1. 环境准备

```bash
# 克隆仓库
git clone git@github.com:gavincjzou/ai_quant.git
cd ai_quant

# 创建 Python 3.11 虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 安装依赖（推荐用 uv 加速 10 倍）
pip install -r requirements.txt
# 或：uv pip install -r requirements.txt
```

### 2. 配置 API 凭证

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑填入 LongPort OpenAPI 凭证
# 获取方式：https://open.longportapp.com
vim .env
```

`.env` 内容：
```ini
LONGPORT_APP_KEY=your_app_key_here
LONGPORT_APP_SECRET=your_app_secret_here
LONGPORT_ACCESS_TOKEN=your_access_token_here
LOG_LEVEL=INFO
TRADE_MODE=paper
```

### 3. 配置企业微信告警（可选）

```bash
# 复制并编辑
cp config/monitor.yaml config/monitor.local.yaml
vim config/monitor.local.yaml
# 填入你的 Webhook URL
```

### 4. 下载历史数据

```bash
# 下载 watchlist 28 只标的 4 年日线
python scripts/fetch_data.py \
  --symbols "AAPL.US,MSFT.US,NVDA.US,..." \
  --days 1000
```

### 5. 运行回测

```bash
# 单策略回测
python scripts/run_backtest.py --strategy ma_cross --symbol AAPL.US

# 批量筛选所有标的×所有策略
python scripts/screen_watchlist.py
```

### 6. 启动 Paper Trading

```bash
# 方式 A：单次扫描（推荐日常使用）
./run_daily.sh

# 方式 B：常驻 daemon（未来上云场景）
python scripts/run_paper_trade.py
```

### 7. 查看 Dashboard

```bash
./run_dashboard.sh              # 生成并打开浏览器
# 或直接打开 output/dashboard.html
```

### 8. 安装 launchd 守护进程（macOS）

```bash
cp scripts/com.gavin.aiquant.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gavin.aiquant.plist

# 验证
launchctl list com.gavin.aiquant.daily-scan
```

---

## 📂 项目结构

```
ai_quant/
├── config/                      # 配置
│   ├── strategies.yaml          # 策略参数 + watchlist + per_symbol 映射
│   ├── risk.yaml                # 风控参数（止损、仓位、熔断）
│   ├── settings.yaml            # 全局设置
│   ├── monitor.yaml             # 监控通道开关（进 git）
│   └── monitor.local.yaml       # 企业微信 Webhook（gitignored）
│
├── src/
│   ├── data/                    # 数据层
│   │   ├── longport_client.py   # LongPort API 封装
│   │   ├── yfinance_client.py   # yfinance 备用源
│   │   ├── database.py          # SQLite 管理
│   │   ├── market_calendar.py   # 美股交易日历
│   │   └── trading_state.py     # 跨进程状态 KV
│   │
│   ├── strategy/                # 策略层
│   │   ├── base_strategy.py
│   │   ├── ma_cross_strategy.py
│   │   ├── rsi_strategy.py
│   │   ├── momentum_strategy.py
│   │   └── strategy_manager.py  # per_symbol 映射入口
│   │
│   ├── backtest/                # 回测层
│   │   └── engine.py            # backtrader 封装
│   │
│   ├── risk/                    # 风控层
│   │   ├── position_sizer.py    # 仓位计算
│   │   ├── stop_loss.py         # ATR 442 止损
│   │   └── risk_manager.py      # 熔断 + 日损限额
│   │
│   ├── trader/                  # 交易层
│   │   ├── paper_trader.py      # 模拟交易（跨进程持久化）
│   │   ├── live_trader.py       # 实盘交易
│   │   ├── order_manager.py     # 订单管理
│   │   └── scheduler.py         # APScheduler 编排
│   │
│   └── monitor/                 # 监控层
│       ├── alerts.py            # 告警总线
│       ├── wecom_channel.py     # 企业微信通道
│       ├── daily_reconciliation.py  # 每日对账
│       └── dashboard.py         # Dashboard 数据聚合
│
├── scripts/                     # 脚本
│   ├── run_paper_trade.py       # 主入口（--daily-scan 模式）
│   ├── run_backtest.py          # 回测入口
│   ├── screen_watchlist.py      # 批量回测筛选
│   ├── backfill_equity_curve.py # 净值曲线补回
│   ├── build_dashboard.py       # HTML Dashboard 生成
│   ├── fetch_data.py            # 数据下载
│   ├── manual_close_positions.py # 手动平仓工具
│   └── com.gavin.aiquant.plist  # launchd 配置
│
├── tests/                       # 单元测试
│   ├── test_strategies.py
│   ├── test_risk.py
│   └── test_data.py
│
├── docs/obsidian-vault/         # Obsidian 知识库
│   ├── ln-量化学习/             # 学习笔记（8 篇）
│   └── dev-系统开发/            # 开发文档（9 篇）
│
├── daily/                       # 每日复盘
│   ├── README.md                # 复盘体系说明
│   ├── TEMPLATE.md              # 模板
│   └── YYYY-MM-DD.md            # 每日记录
│
├── run_daily.sh                 # 日扫快捷脚本
├── run_dashboard.sh             # Dashboard 快捷脚本
├── requirements.txt
├── setup.py
└── README.md
```

---

## 🏁 开发里程碑

| 阶段 | 日期 | 主题 | 关键成果 |
|------|------|------|---------|
| **1** | 2026-04-15 | 基础架构 | 项目骨架 + LongPort 接入 + SQLite |
| **2** | 2026-04-17 | 策略层 | MA/RSI/Momentum + ATR 442 + PositionSizer |
| **3** | 2026-04-18 | 回测优化 | backtrader + PerShareCommission + 敏感度分析 |
| **4** | 2026-04-19 | 风控完善 | 熔断 + 日损限额 + 熔断阈值统一 |
| **5** | 2026-04-20 | 监控层 | APScheduler + 对账 + 基础 Dashboard |
| **6** | 2026-04-21 | A/B 对比 | 54 次策略升级 A/B，回退 legacy |
| **7** | 2026-04-22 | Paper 启动 | 日线扫描模式 + 企业微信 + launchd |
| **8** | 2026-04-23 | 生产化 | **跨进程状态持久化** + per_symbol 映射 + Cyberpunk Dashboard + Git 上线 |
| **9** | *规划中* | 多因子 | 6 因子模型 + Top-N 换仓（阶段 7 数据积累 2 周后启动）|

详见 `docs/obsidian-vault/dev-系统开发/`

### 📊 当前运行状态

- **总资产**：$799,094（初始 $800,000，Paper Trading 跑了 2 天）
- **持仓**：MSFT / NVDA / META / TSM / AVGO（全部 Momentum 策略匹配）
- **Watchlist**：28 只跨 8 行业
- **策略映射**：RSI 12 / MA 6 / Momentum 10

---

## 📝 开发约定

### Commit 规范（约定式）

```
<type>: <简短描述>

<可选的详细说明>
```

**Type 清单**：

| Type | 用途 | 例子 |
|------|------|------|
| `feat` | 新功能 | `feat: 添加财报数据接入` |
| `fix` | 修 bug | `fix: PaperTrader 状态持久化` |
| `refactor` | 重构 | `refactor: StrategyManager 抽象基类` |
| `docs` | 文档 | `docs: 完善阶段 8 开发记录` |
| `test` | 测试 | `test: 补全 load_state 单元测试` |
| `style` | 样式 | `style: Dashboard Cyberpunk 升级` |
| `chore` | 杂项 | `chore: launchd 改 22:00 触发` |

### 分支策略

- **单人开发**：`main` 分支直接 commit
- **试验性大改**：`experiment/xxx` 分支
- **每天至少 1 次 push**（备份）

### 安全红线

**永远不进 Git 的文件**：
- `.env`（API 凭证）
- `config/*.local.yaml`（企业微信 Webhook）
- `data_cache/*`（持仓 + 交易记录）
- `output/*`（对账 + 告警日志）
- `.venv/`（Python 虚拟环境）

**每次 `git add` 前必看 `git status`**。

### 测试策略

- 核心模块必须有单元测试（PaperTrader / StopLossManager / RiskManager）
- 回测结果变动大于 5% 必须复核
- 参数调整前跑 A/B 对比，至少 10 只标的 × 2 种配置

### 风控原则

- **数据优先，观点其次**：任何策略必须有回测数据支撑
- **先搞定再优化**：MVP → 迭代，别追求完美
- **风控第一**：最大回撤 > 20% 必须停下排查
- **稳定区间 > 理论最优**：参数别过拟合

---

## 💻 常用命令

### 日常运维

```bash
# 每天跑一次（launchd 会自动，也可手动）
./run_daily.sh

# 看 Dashboard
./run_dashboard.sh

# 看今日对账
cat output/reconciliation/$(date +%Y-%m-%d).md

# 查 launchd 状态
launchctl list com.gavin.aiquant.daily-scan

# 看最近 100 行日志
tail -100 logs/paper_trading.log
```

### 开发调试

```bash
# 跑单元测试
pytest tests/ -v

# 跑回测
python scripts/run_backtest.py --strategy momentum --symbol NVDA.US

# 筛选 watchlist（生成 CSV）
python scripts/screen_watchlist.py

# 手动平仓（带告警）
python scripts/manual_close_positions.py --close AAPL.US --dry-run
python scripts/manual_close_positions.py --close AAPL.US

# 补回净值曲线
python scripts/backfill_equity_curve.py

# 查看所有持仓
python scripts/manual_close_positions.py --list
```

### Git 日常

```bash
# 每天工作完成
git add . && git commit -m "feat: xxx" && git push

# 看最近提交
git log --oneline -10

# 看某次 commit 改了啥
git show <hash>

# 撤销还没 push 的最后一次 commit（保留改动）
git reset --soft HEAD~1
```

---

## ⚠️ 风险提示

**⚠️ 量化交易存在市场风险，本系统仅供学习研究，不构成投资建议。**

- 回测收益不代表未来真实收益
- 建议先用 Paper Trading 充分验证（至少 1 个月）
- 实盘从小金额开始（建议 ≤ 总资金的 10%）
- 任何策略都可能亏损，**绝对不用借贷资金**
- API 密钥妥善保管，不要 commit 到 git

**📜 License**：Private · 未开源 · 个人研究用途

---

<div align="center">

_Built with ☕ and 📊 by Gavin · 2026_

[📧 联系](mailto:gavincjzou@tencent.com) · [📚 文档](docs/obsidian-vault/) · [🌌 Dashboard](output/dashboard.html)

</div>

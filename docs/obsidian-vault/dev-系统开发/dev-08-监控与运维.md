# dev-08 监控与运维

> **创建日期**：2026-04-22（阶段5）
> **相关**：[[dev-04-回测记录]] | [[dev-05-风控规则]] | [[dev-06-模拟交易日志]]

## 1. 架构概览

```
    scripts/run_paper_trade.py  (Orchestrator)
                   │
                   ├── Scheduler (APScheduler) ── 5 类 Job
                   │     ├── pre_market      09:25 ET
                   │     ├── intraday_open   09:35 ET
                   │     ├── intraday_monitor 每30分钟
                   │     ├── intraday_pre_close 15:45 ET
                   │     └── post_close      16:05 ET
                   │
                   ├── PaperTrader (订单执行)
                   │
                   ├── DailyReconciliation (每日对账)
                   │
                   └── AlertManager (告警三通道)
                         ├── LOG       (output/alerts.log, 必开)
                         ├── TELEGRAM  (TELEGRAM_BOT_TOKEN, 可选)
                         └── EMAIL     (ALERT_SMTP_*, 可选)
```

## 2. 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Scheduler | `src/trader/scheduler.py` | APScheduler Job 编排 + 交易日判断 + 异常告警 |
| AlertManager | `src/monitor/alerts.py` | 三通道告警（LOG/TG/Email），自动降级 |
| DailyReconciliation | `src/monitor/daily_reconciliation.py` | 收盘对账 + 生成 Markdown 报告 |
| Dashboard CLI | `scripts/dashboard.py` | 持仓/风控/告警/回测查询 |
| Orchestrator | `scripts/run_paper_trade.py` | 串联所有组件 |

## 3. 告警等级与通道

| Level | 场景 | 推送 |
|-------|------|------|
| INFO | 订单成交、盘前数据就绪、每日对账摘要 | LOG + Telegram |
| WARNING | 止损/TP 触发、数据缺失、对账异常 | LOG + Telegram + Email |
| CRITICAL | 熔断、Scheduler Job 异常、连接失败 | LOG + Telegram + Email |

### 配置告警凭证（.env 或环境变量）

```bash
# Telegram（推荐，秒级推送）
export TELEGRAM_BOT_TOKEN="1234:ABC..."
export TELEGRAM_CHAT_ID="你的 chat_id"

# 邮件（WARNING+ 场景备用）
export ALERT_SMTP_HOST="smtp.qq.com"
export ALERT_SMTP_PORT="465"
export ALERT_SMTP_USER="xxx@qq.com"
export ALERT_SMTP_PASS="邮箱授权码"
export ALERT_EMAIL_TO="xxx@example.com,yyy@example.com"
```

不配任何凭证时：**自动降级到纯 LOG**，所有告警落盘到 `output/alerts.log`。

## 4. 每日对账流程

`DailyReconciliation.run()` 在 16:05 ET 触发：

1. **交易汇总**：当日 N 笔买卖、按策略/标的分布、总手续费
2. **持仓快照**：总资产、现金、市值、累计收益、每只股票浮盈亏
3. **风控状态**：当日 PnL%、最大回撤、熔断标志
4. **止损止盈追踪**：每个持仓的 stop/TP1/TP2/TP3 触发状态
5. **对账校验**：
   - PaperTrader.positions 与 StopLossManager._positions 一致性
   - 熔断状态
   - 日亏损是否接近/超过 3% 日限
6. **报告输出**：`output/reconciliation/YYYY-MM-DD.md`
7. **告警发送**：按异常最高等级发送 Telegram/Email

## 5. 常用运维命令

```bash
# 启动流程
python scripts/run_paper_trade.py --preview                      # 查看 Jobs
python scripts/run_paper_trade.py --once scan                    # 单次 scan 调试
nohup python scripts/run_paper_trade.py > logs/paper.log 2>&1 &  # 后台运行

# 状态查看
python scripts/dashboard.py --all
python scripts/dashboard.py --positions
python scripts/dashboard.py --trades 20
python scripts/dashboard.py --alerts 30
python scripts/dashboard.py --recon 2026-04-22
python scripts/dashboard.py --backtest 10

# 日志查看
tail -f logs/system_2026-04-22.log
tail -f logs/trade_2026-04-22.log
tail -f logs/error_2026-04-22.log

# 停止 Scheduler
# 前台：Ctrl+C（会触发 Scheduler 关闭告警）
# 后台：kill $(pgrep -f run_paper_trade)
```

## 6. 故障处置手册

| 问题 | 排查步骤 |
|------|--------|
| Scheduler 不触发 Job | 1) 检查是否交易日 2) 检查 ET 时区 3) 查 `logs/system_*.log` |
| 订单全部被拒单 | 1) `--once scan` 手动跑 2) 看是否 max_positions=5 塞满 3) 检查 risk.yaml |
| ATR 不生效 | 1) 确认 data_map 不为空 2) 确认 PaperTrader._recent_atr 有数据 3) 看 risk.yaml.position.mode=risk_based_atr |
| 告警没推送 | 1) `output/alerts.log` 是否写入 2) 环境变量是否配 3) 测试脚本 `python -c 'from src.monitor.alerts import *; AlertManager().info("test")'` |
| 对账异常 SL_MISSING | PaperTrader 持仓但 StopLossManager 丢了 → 重启 Orchestrator 或手动调 trader.stop_loss_manager.track_position(...) |
| 熔断触发 | 必须人工 review 后手动 risk_manager._is_halted=False 解除 |

## 7. 阶段5 升级日志

| 日期 | 变更 |
|------|------|
| 2026-04-22 | 新增 `src/monitor/alerts.py` 三通道告警 |
| 2026-04-22 | 新增 `src/trader/scheduler.py` APScheduler 编排 |
| 2026-04-22 | 新增 `src/monitor/daily_reconciliation.py` 每日对账 |
| 2026-04-22 | 新增 `scripts/dashboard.py` CLI 监控 |
| 2026-04-22 | 重写 `scripts/run_paper_trade.py` 用 Scheduler 替代 while-True |
| 2026-04-22 | risk.yaml 切换：`stop_loss.mode=atr_442` + `position.mode=risk_based_atr` |
| 2026-04-22 | 按策略风险预算从扫描结果更新：MA 2.25% / RSI 2.5% / Momentum 4.38% |
| 2026-04-22 | max_single_position_pct 从 20% → 45%（适配更大 risk_pct） |
| 2026-04-22 | active_strategies 启用 momentum（原来注释） |

## 8. 下一步运维 Backlog

- [ ] 配置 Telegram Bot Token（当前仅 LOG）
- [ ] 配置邮件告警（QQ 邮箱 SMTP）
- [ ] 启动 Paper Trading 阻塞模式运行 2 周
- [ ] 每周末 review 对账报告 + 周度回顾填入 [[dev-06-模拟交易日志]]
- [ ] 2 周后评估是否进入 LiveTrader 实盘（见 [[dev-07-实盘交易日志]]）

---

## 9. 阶段7：日线扫描模式（方案 C）

### 9.1 设计动机

Gavin 的电脑是**个人电脑**，工作日晚 21:00+ 才开机，**不可能 24/7 跑 daemon**。  
美股交易时段是北京时间 21:30-04:00，daemon 模式不适合。

**方案 C 核心思想**：日线策略不需要盘中实时跑，**每天美东收盘后扫一次就够了**。

### 9.2 启动方式

```bash
cd ~/ai_quant
./run_daily.sh                # 正常跑（推荐）
./run_daily.sh --dry-run      # 只看 gap 不执行
```

或直接调用：
```bash
.venv/bin/python scripts/run_paper_trade.py --daily-scan
```

### 9.3 核心流程

1. 读 `trading_state.last_scan_date` （SQLite，跨进程持久化）
2. 调 `USMarketCalendar.last_closed_trading_day(buffer=30min)` 拿"已收盘最近交易日"
3. 计算 gap 列表（最多回补 14 天，防首次跑爆）
4. 对每个 gap 日：scan → 对账 → 推送企业微信
5. 写回 last_scan_date
6. 退出（不常驻）

### 9.4 严格模式逻辑

- 美东今日 < 16:30：今日不算"已收盘"，target = 昨天
- 美东今日 ≥ 16:30：今日算已收盘，target = 今天
- 周五跑 → target = 周五；周六跑 → target = 周五（不变）；周一晚跑 → 补周五（如周一已收盘则补到周一）

### 9.5 幂等性

- 第二次跑相同日期 → 检测 `target ≤ last_scan` → 跳过 + 推送"无需补跑"消息
- 安全可重复触发，不会重复下单

### 9.6 自动化触发（可选）

#### 方案 1：WorkBuddy 自动化（默认推荐）

已配置：每周一到周五 21:30 北京时间触发，自动跑 `./run_daily.sh` 并汇报结果。

#### 方案 2：macOS launchd（兜底）

```bash
cp scripts/com.gavin.aiquant.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gavin.aiquant.plist
```

效果：
- 登录 Mac 立即跑一次
- 每天 21:30 跑一次
- 日志写到 `logs/launchd.{out,err}.log`

### 9.7 故障排查

| 症状 | 排查方向 |
|------|--------|
| 没收到企业微信 | 1. `tail -f output/alerts.log` 看日志 2. 检查 `monitor.local.yaml` Webhook 是否还有效 3. 用 curl 直接 POST 测试 |
| 同一天重复跑没反应 | 正常，幂等保护，看终端会输出"无需补跑" |
| target_date 总是停留在前一天 | 美东未收盘+30min 缓冲，凌晨 04:30 之后再跑 |
| 首次跑只补 1 天不是历史所有 | 设计如此，避免首次拉太多。如需手动补历史用 `--once all` |
| 日志里有 ERROR | 看 `logs/paper_trading.log` 完整堆栈 |

### 9.8 命令速查

```bash
# 日扫
./run_daily.sh

# 看 gap 不执行
./run_daily.sh --dry-run

# 手动跑某个 Job（调试）
.venv/bin/python scripts/run_paper_trade.py --once scan
.venv/bin/python scripts/run_paper_trade.py --once post_close

# 看告警日志
tail -50 output/alerts.log

# 看对账报告
ls -t output/reconciliation/ | head -5

# 查 trading_state
sqlite3 data_cache/quant.db "SELECT * FROM trading_state;"
```

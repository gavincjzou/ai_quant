# 📝 模拟交易日志

> 建议运行模拟交易 2-4 周后再考虑实盘。
>
> **阶段5（2026-04-22）** 起本系统已升级为 APScheduler 编排的全功能版。
> 详见 [[dev-08-监控与运维]]。

## 启动命令

```bash
# 预览 Jobs 调度表（不启动）
python scripts/run_paper_trade.py --preview

# 手动触发一次单个 Job（调试）
python scripts/run_paper_trade.py --once scan           # 仅扫描一次
python scripts/run_paper_trade.py --once post_close     # 仅跑一次对账
python scripts/run_paper_trade.py --once all            # 完整跑一遍 4 个 Job

# 正式启动（阻塞，由 Scheduler 编排全天 5 个 Job）
python scripts/run_paper_trade.py --capital 800000

# Dashboard 查看状态
python scripts/dashboard.py --all
python scripts/dashboard.py --alerts 20
python scripts/dashboard.py --recon 2026-04-22
```

## Scheduler 调度表（美东时间 ET）

| Job | 时间 | 职责 |
|-----|------|------|
| pre_market | 09:25 | 拉数据 + 健康检查 |
| intraday_open | 09:35 | 开盘信号扫描 |
| intraday_monitor | 每 30 分钟 | 止损止盈检查（盘中 9-15 点） |
| intraday_pre_close | 15:45 | 收盘前最后一次信号扫描 |
| post_close | 16:05 | 盘后对账 + 报告 + 告警汇总 |

## 每日记录模板

### YYYY-MM-DD

**市场环境**: _（描述当天大盘走势、重要新闻）_

**交易记录**:

| 时间 | 标的 | 方向 | 数量 | 价格 | 策略 | 信号原因 |
|------|------|------|------|------|------|---------|
| | | | | | | |

**持仓快照**:

| 标的 | 数量 | 成本 | 现价 | 浮动盈亏 | 盈亏% |
|------|------|------|------|---------|-------|
| | | | | | |

**组合指标**:
- 总资产: $___
- 现金: $___
- 市值: $___
- 当日收益: ___
- 累计收益: ___%

**当日反思**: _（策略表现如何？有无需要调整的参数？）_

---

## 周度总结模板

### W__ (MM/DD - MM/DD)

- 周收益率: ___%
- 交易笔数: ___
- 胜率: ___%
- 最大单笔盈利: $___
- 最大单笔亏损: $___
- 风控触发次数: ___
- 是否需要调参: ___
- 是否可以进入实盘: ___

---

## 模拟交易总结

_（模拟交易结束后填写，作为是否进入实盘的决策依据）_

- 总运行天数: ___
- 总收益率: ___%
- 最大回撤: ___%
- 夏普比率: ___
- 策略是否稳定: ___
- 风控是否有效: ___
- **结论**: 是否进入实盘 ✅/❌

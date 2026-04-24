# 🛡️ 风控规则

## 当前风控参数

> 配置文件: `config/risk.yaml`
> 所有参数可随时修改，无需改代码。

### 仓位管理

| 规则 | 阈值 | 说明 |
|------|------|------|
| 单票最大仓位 | 总资产 20% | 防止单只股票风险过高 |
| 默认单笔仓位 | 总资产 10% | 每次买入的默认金额比例 |
| 最大持仓数量 | 5 只 | 小资金集中持仓 |
| 最小下单金额 | $50 | 低于此金额不下单 |

### 止损止盈

| 规则 | 阈值 | 说明 |
|------|------|------|
| 个股止损 | -5% | 亏损超5%自动平仓 |
| 个股止盈 | +15% | 盈利超15%自动平仓 |
| 追踪止损 | 5% | 从最高价回落5%平仓 |

### 每日限制

| 规则 | 阈值 | 说明 |
|------|------|------|
| 单日最大亏损 | 总资产 3% | 触发当日停止交易 |
| 单日最大交易笔数 | 10 | 防止过度交易 |

### 组合级别

| 规则 | 阈值 | 说明 |
|------|------|------|
| 最大回撤熔断 | 20% | 触发全面暂停交易（阶段1从10%上调，匹配442止盈方案的回撤容忍度） |
| 隔夜持仓 | ✅ 允许 | 美股 PDT 规则限制日内交易 |
| 财报日窗口 | 前后 1 天 | 禁止财报日附近新开仓 |

## 风控执行流程

```
交易信号 → 风控校验（串联执行）
  1. 熔断检查 → 是否已触发组合回撤熔断
  2. 单日限额 → 今日亏损/交易次数是否超限
  3. 持仓数量 → 是否超过最大持仓数
  4. 单票仓位 → 该标的是否已达上限
  5. 最小金额 → 下单金额是否太小
  6. 财报窗口 → 是否在财报日附近
  → 全部通过 → 计算批准数量 → 执行交易
  → 任一拦截 → 记录原因 → 拒绝交易
```

## 风控事件记录

| 日期 | 事件类型 | 标的 | 触发规则 | 处理 |
|------|---------|------|---------|------|
| _待记录_ | | | | |

## 阈值调整日志

| 日期 | 参数 | 旧值 | 新值 | 原因 |
|------|------|------|------|------|
| 2026-04-15 | 所有参数 | - | 初始值 | 系统初始化 |
| 2026-04-21 | max_drawdown_pct | 10% | 20% | 阶段1升级：匹配ATR+442止盈方案的回撤容忍度，避免熔断过于敏感 |
| 2026-04-21 | stop_loss.mode | legacy | 可切换 (legacy / atr_442) | 阶段2升级：引入ATR动态止损 |
| 2026-04-21 | position.mode | fixed_pct | 可切换 (fixed_pct / risk_based_atr / legacy_cash95) | 阶段2升级：引入单笔风险反算仓位 |

---

## 阶段 2 升级：atr_442 模式落地说明

> **更新日期**：2026-04-21
> **相关回测数据**：[[dev-04-回测记录#阶段 2：风控升级（2026-04-21）]]
> **对应学习笔记**：[[ln-06-风险管理与仓位控制]]

### 核心思路

把"固定百分比止损"升级为"**ATR 动态止损 + 4-4-2 分批止盈 + 单笔风险反算仓位**"三层联动：

1. **仓位大小**由单笔风险预算反推：`shares = equity × risk% / (ATR × stop_mult)`
2. **止损距离**按 ATR × 倍数动态计算（趋势强时止损宽，趋势弱时止损紧）
3. **分批止盈**让盈利分三档落袋：TP1 40% → TP2 40% → TP3 20%，TP1 后止损上移保本

### 配置开关（config/risk.yaml）

```yaml
stop_loss:
  mode: "legacy"        # 切换：legacy | atr_442
  atr_442:
    atr_stop_mult: 2.0   # 全局 ATR 止损倍数
    tp1_rr: 1.0          # TP1 = 入场价 + ATR × tp1_rr
    tp2_rr: 2.0
    tp3_rr: 3.0
    tp1_size_pct: 0.40   # TP1 平仓比例
    tp2_size_pct: 0.40
    tp3_size_pct: 0.20
    move_stop_to_breakeven_after_tp1: true  # TP1 后止损上移到入场价

position:
  mode: "fixed_pct"     # 切换：fixed_pct | risk_based_atr | legacy_cash95
  single_trade_risk_pct: 0.02  # 默认单笔风险预算 (总资产 2%)

per_strategy_overrides:   # 按策略分级参数
  ma_cross:
    single_trade_risk_pct: 0.015   # MA 更保守 1.5%
    atr_period: 20                 # 中线用长周期 ATR
    atr_stop_mult: 2.5
    tp1_rr: 1.5                    # 中线 TP1 更远
  rsi:
    single_trade_risk_pct: 0.02    # RSI 短线 2%
    atr_period: 14
    atr_stop_mult: 2.0
    tp1_rr: 1.0                    # 短线 TP1 更近
  momentum:
    single_trade_risk_pct: 0.025   # 动量确定性高 2.5%
    atr_period: 14
    atr_stop_mult: 2.0
    tp1_rr: 1.0
```

### 按策略分级参数（交易画像）

| 策略 | 持仓周期 | 风险预算 | ATR 周期 | stop_mult | TP1 距离 |
|------|---------|---------|---------|-----------|---------|
| **MA Cross** | 2-8 周（中线） | 1.5% | 20 | 2.5×ATR | 1.5×ATR |
| **RSI** | 3-10 天（短线） | 2.0% | 14 | 2.0×ATR | 1.0×ATR |
| **Momentum** | 3-10 天（短线） | 2.5% | 14 | 2.0×ATR | 1.0×ATR |

### 切换方法

```bash
# 命令行覆盖
python scripts/run_backtest.py --strategy ma_cross --symbol AAPL.US \
    --sl-mode atr_442 --pos-mode risk_based_atr

# A/B 对比全自动
python scripts/run_ab_comparison.py --full    # 9 标的 × 3 策略 × 2 模式

# 参数敏感性扫描
python scripts/run_sensitivity_442.py
```

### 异常回退策略

- **ATR = NaN / 0 / None**：自动回退 `fixed_pct` 模式 + warning 日志
- **数据不足 < 20 根 bar**：ATR 无法计算，整段回退
- **442 部分平仓 size=0**：整数股四舍五入保底至少 1 股

### 何时用 legacy，何时用 atr_442

| 场景 | 推荐 mode | 理由 |
|------|---------|-----|
| 稳健型账户（>50万 HKD） | **atr_442 + risk_based_atr** | 守住本金，回撤降低 50%+ |
| 激进型账户（<5万 HKD） | **legacy_cash95** | 小资金靠高仓位搏大收益 |
| A/B 对比基线复现 | **legacy_cash95** | 严格复现旧行为 |
| 高波动股（TSLA/NVDA） | **atr_442** | 动态止损最能保护 |
| 稳定 ETF（SPY/QQQ） | 都可 | 差异不大 |

### 实盘落地状态

- ✅ 回测层：完成（`src/backtest/engine.py` 已注入 PositionSizer + StopLossManager）
- ✅ 风控层：完成（`src/risk/position_sizer.py`、`src/risk/stop_loss.py`）
- ✅ 指标层：完成（`src/utils/indicators.py` 提供 calc_atr / calc_rsi）
- 🚧 实盘层：PaperTrader/LiveTrader ATR 集成（阶段 4 进行中）
- ⚠️ 监控层：仍为空，下一阶段搭建

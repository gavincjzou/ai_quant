---
title: dev-09 多因子选股规划
tags: [strategy, multifactor, planning, stage8]
created: 2026-04-22
status: 设计阶段（未实施）
---

# dev-09 多因子选股规划

> **背景**：阶段6 [[dev-04-回测记录]] 中 MA+量能、RSI+MA50 升级 A/B 显示，**单指标+单确认层架构的天花板就是年化 5-10%**。要真正跑赢 QQQ（年化 ~18%），必须升级到**多因子打分选股**。
>
> **状态**：设计文档，等阶段7 Paper Trading 跑 2-4 周后基于真实数据决定是否启动。

---

## 1. 设计目标

| 维度 | 当前（阶段5-6） | 多因子目标（阶段8） |
|------|---------------|------------------|
| 选股逻辑 | 9 只 watchlist 全跑，策略各跑各的 | **每日打分 → Top5 持仓** |
| 信号生成 | 单指标判断 | **多因子加权综合** |
| 仓位决策 | 按策略 risk_pct 算 | **按打分高低 + Kelly 简化版** |
| 持仓周期 | 信号驱动（不固定）| **周/月度再平衡** |
| 预期年化 | 5-10% | **15-20%（目标）** |
| 最大回撤 | 13-15% | **<= 20%（放宽）** |

---

## 2. 因子体系（5 维度，每维度 100 分）

### 2.1 价值因子（Value）— 占 20%

| 子因子 | 计算 | 数据源 | 方向 |
|--------|------|--------|------|
| PE 倒数 | 1 / PE Ratio | LongPort fundamentals | 越高越好 |
| EV/EBITDA | 企业价值 / 息税折旧前利润 | LongPort | 越低越好 |
| Free Cash Flow Yield | FCF / Market Cap | LongPort | 越高越好 |

**说明**：科技股 PE 普遍偏高，Value 权重不宜过大；可在配置层按行业差异化。

### 2.2 质量因子（Quality）— 占 20%

| 子因子 | 计算 | 数据源 | 方向 |
|--------|------|--------|------|
| ROE | 净资产收益率 | 财报 | 越高越好 |
| ROA | 总资产收益率 | 财报 | 越高越好 |
| 净利润率 | Net Margin | 财报 | 越高越好 |
| 负债率 | Debt-to-Equity | 财报 | 越低越好 |

### 2.3 动量因子（Momentum）— 占 25%

| 子因子 | 计算 | 数据源 | 方向 |
|--------|------|--------|------|
| 6M Return | 过去 126 日收益率 | K线 | 越高越好 |
| 3M Return | 过去 63 日收益率 | K线 | 越高越好 |
| 12M-1M | 过去 12 月扣去最近 1 月（剔除反转）| K线 | 越高越好 |

**学术依据**：Jegadeesh-Titman (1993) 经典动量论文，6-12月动量在美股长期有效。

### 2.4 技术因子（Technical）— 占 20%

| 子因子 | 计算 | 数据源 | 方向 |
|--------|------|--------|------|
| MA 趋势 | Price > MA50 ? +1 : -1 | K线 | 趋势向上 |
| RSI 健康度 | 30 < RSI < 70 ? 满分 : 衰减 | K线 | 不极端 |
| MACD 信号 | Hist > 0 ? +1 : -1 | K线 | 多头 |
| ADX 强度 | ADX > 25 ? 趋势市 | K线 | 越强越好 |

### 2.5 流动性 + 波动率（Liquidity & Risk）— 占 15%

| 子因子 | 计算 | 数据源 | 方向 |
|--------|------|--------|------|
| 日均成交额 | 20日 turnover 均值 | K线 | 越高越好（避免流动性陷阱）|
| 历史波动率 | 60日收益率标准差 | K线 | 越低越好（同收益首选低波） |
| Beta | 相对 SPY 60日 Beta | K线 + SPY | 中性偏低 |

---

## 3. 评分归一化方案

### 3.1 单因子归一化

对每个因子在**全市场样本**内做 **Z-Score**，然后映射到 [0, 100] 分：

```python
def normalize_factor(values: pd.Series) -> pd.Series:
    """Z-Score 归一化 + Sigmoid 映射到 [0, 100]"""
    z = (values - values.mean()) / values.std()
    # Sigmoid: 1 / (1 + exp(-z))
    return 100 / (1 + np.exp(-z))
```

**为什么用 Sigmoid 不用 MinMax**：
- MinMax 受极值影响大（一只异常股票拉高/拉低整体）
- Z-Score 反映相对位置
- Sigmoid 把 ±2σ 之外的极端值压平，避免单因子主导

### 3.2 大类聚合 + 加权

```
total_score = (
    value_score * 0.20 +
    quality_score * 0.20 +
    momentum_score * 0.25 +
    technical_score * 0.20 +
    liquidity_risk_score * 0.15
)
```

**权重可配置**（`config/multifactor.yaml`）：
- 防御型组合：Quality 30%、Value 25%、Momentum 15%
- 进攻型组合：Momentum 35%、Technical 25%、Quality 15%
- **默认：均衡组合**（上面给的权重）

---

## 4. 选股 + 持仓管理流程

### 4.1 每日打分

```
盘后 → 拉取最新财报/价格 → 跑 5 大类因子 → 归一化 → 加权 → 总分排行
```

### 4.2 持仓决策（周度再平衡）

```python
# 每周一收盘后执行
def rebalance(scores: pd.DataFrame, current_positions: dict) -> List[Trade]:
    top5 = scores.nlargest(5, "total_score")

    # 当前持仓 vs 新 top5
    keep    = set(current_positions.keys()) & set(top5.index)
    sell    = set(current_positions.keys()) - set(top5.index)
    buy     = set(top5.index) - set(current_positions.keys())

    # 按打分 Kelly 简化版分配仓位
    weights = top5["total_score"] / top5["total_score"].sum()
    target_value = portfolio_value * weights

    return generate_trades(keep, sell, buy, target_value)
```

### 4.3 风控集成

- **单票上限**：最大 30%（避免重仓单只）
- **现金缓冲**：保留 10% 不投
- **熔断**：跟阶段5 一致（20% 回撤暂停）
- **止损**：依然走 ATR-442（多因子选股不代表不需要止损）

---

## 5. 实施路径（4 周渐进）

### Week 1：数据接入
- [ ] 接入 LongPort 财报 API（fundamentals）
- [ ] 新增 `src/data/fundamentals_fetcher.py`
- [ ] SQLite 增加 `fundamentals` 表
- [ ] 单测：拉取 9 只 watchlist 的 PE/ROE/Margin

### Week 2：因子计算
- [ ] 新增 `src/strategy/factors/` 子模块
- [ ] 每个因子一个 py 文件：`value.py` / `quality.py` / `momentum.py` / `technical.py` / `liquidity.py`
- [ ] 归一化工具 `src/strategy/factors/normalizer.py`
- [ ] 单测：每个因子用 mock 数据验证

### Week 3：选股引擎
- [ ] `src/strategy/multifactor_selector.py`：聚合打分 + Top5 选股
- [ ] `config/multifactor.yaml`：权重 + 阈值配置
- [ ] 回测：MultiFactorSelector 接入现有 `BacktestEngine`，对比 buy-and-hold QQQ
- [ ] **决策门槛**：年化 ≥ 12% 且 MaxDD ≤ 22% 才算合格

### Week 4：再平衡 + Paper Trading
- [ ] `RebalanceScheduler`：周一收盘触发再平衡
- [ ] 接入 Paper Trading：把 multifactor 加入 active_strategies
- [ ] 跑 2 周观察真实持仓变化

---

## 6. 关键技术挑战

| 挑战 | 风险 | 缓解措施 |
|------|------|---------|
| 财报数据更新滞后 | 因子打分用过期数据 | 每天检查 `last_filed_date`，超期降权 |
| 行业差异巨大 | 用全市场归一化对科技股不公平 | 按行业分组后归一化（Z-Score within sector）|
| 因子失效 | 历史有效不代表未来有效 | 每月重跑因子 IC（Information Coefficient）检验 |
| 换手率过高 | 周度再平衡可能频繁交易 | 加 turnover 缓冲：分数变化 < 5% 不调仓 |
| Survivorship Bias | 只看现在的 9 只标的有幸存者偏差 | 实施时扩大到 SP500 / NASDAQ100 全样本 |

---

## 7. 与现有架构的关系

```
BaseStrategy（保留）
  ├── MACrossStrategy（保留，作为 fallback）
  ├── RSIStrategy（保留）
  ├── MomentumStrategy（保留）
  └── MultiFactorStrategy（阶段8 新增）
        └── 内部组合 5 个因子
        └── 输出 Top5 持仓建议而非"买/卖"信号
```

**关键设计**：`MultiFactorStrategy` 作为**新策略类型**接入，**不破坏**现有单因子策略。可以并行运行 → A/B 对比谁的真实 PnL 更好。

---

## 8. 推迟到阶段8 的理由

1. **数据准备复杂**：需要先接入 LongPort 财报 API（阶段7 还没做）
2. **真实数据驱动**：Paper Trading 跑 2-4 周后能让我们知道：
   - 哪些指标在我们的 watchlist 里真的有信号
   - 实际换手率/滑点是多少
3. **风险隔离**：先验证现有架构的稳定性，再叠加复杂逻辑

---

## 9. 触发条件（什么时候启动阶段8）

- ✅ Paper Trading 跑满 2 周（10 个交易日）
- ✅ 至少 5 笔成交，可统计胜率/滑点
- ✅ 监控告警链路无误报
- ✅ 多因子设计经 Gavin Review 确认

满足后即可启动 Week 1 数据接入。

---

## 10. 参考资料

- Jegadeesh & Titman (1993) "Returns to Buying Winners and Selling Losers"
- Fama & French (1993) "Common Risk Factors in Returns on Stocks and Bonds"
- AQR "Quality Minus Junk" (2014)
- 学习笔记 [[ln-05-多因子模型基本面分析]] —— 已有理论基础

---

_文档状态：设计完成，等阶段7 验证后启动实施。_

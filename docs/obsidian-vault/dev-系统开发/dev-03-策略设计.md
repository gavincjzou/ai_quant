# 📈 策略设计

## 策略框架

所有策略继承 `BaseStrategy` 抽象基类，实现统一接口：

```python
class BaseStrategy(ABC):
    def init(self, config: dict) -> None: ...
    def generate_signal(self, symbol: str, data: pd.DataFrame) -> Optional[TradeSignal]: ...
    def on_order_filled(self, order_info: dict) -> None: ...
```

## 内置策略

### 1. 均线交叉策略 (MA Cross)

| 参数 | 默认值 | 说明 |
|------|-------|------|
| short_period | 5 | 短期均线周期 |
| long_period | 20 | 长期均线周期 |
| signal_type | SMA | SMA 或 EMA |

**逻辑**:
- 金叉（短均线上穿长均线）→ BUY
- 死叉（短均线下穿长均线）→ SELL

**适用场景**: 趋势明显的市场

### 2. RSI 反转策略

| 参数 | 默认值 | 说明 |
|------|-------|------|
| period | 14 | RSI 计算周期 |
| overbought | 70 | 超买阈值 |
| oversold | 30 | 超卖阈值 |

**逻辑**:
- RSI < 30 (超卖) → BUY
- RSI > 70 (超买) → SELL

**适用场景**: 震荡市场

### 3. 动量策略 (Momentum)

| 参数 | 默认值 | 说明 |
|------|-------|------|
| lookback_period | 20 | 回看周期 |
| buy_threshold | 0.05 | 买入阈值 (5%) |
| sell_threshold | -0.03 | 卖出阈值 (-3%) |

**逻辑**:
- N 日收益率 > 5% → BUY
- N 日收益率 < -3% → SELL

**适用场景**: 趋势跟踪

## 信号执行规则

- **信号产生时机**: 收盘价计算
- **执行时机**: 下一根 K 线开盘价
- **未来函数**: 严格禁止
- **多策略冲突**: 取置信度最高的信号

## 自定义策略指南

1. 在 `src/strategy/` 创建新文件
2. 继承 `BaseStrategy`
3. 实现 `init()` 和 `generate_signal()`
4. 在 `strategy_manager.py` 的 `STRATEGY_REGISTRY` 注册
5. 在 `config/strategies.yaml` 添加参数配置

## 策略优化记录

_（后续回测过程中持续记录参数调优结果）_

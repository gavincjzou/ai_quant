# 📊 数据层设计

## 数据源

| 数据类型 | 来源 | 接口 | 说明 |
|---------|------|------|------|
| 历史 K 线 | 长桥 OpenAPI | `get_history_kline()` | 日/周/月/分钟 K 线 |
| 实时行情 | 长桥 OpenAPI | `get_realtime_quote()` / WebSocket | 最新价/量/额 |
| 账户数据 | 长桥 OpenAPI | `get_account_balance()` / `get_positions()` | 持仓/资金 |
| 财报数据 | WorkBuddy neodata | 自然语言查询 | 营收/利润/估值 |
| 宏观数据 | WorkBuddy finance-data | 结构化 API | GDP/CPI/利率 |

## 数据存储

- **SQLite** (`data_cache/quant.db`): 结构化存储
  - `kline_data`: K 线数据
  - `trade_records`: 交易记录
  - `backtest_results`: 回测结果
  - `daily_performance`: 每日绩效
  - `position_snapshots`: 持仓快照
- **CSV** (`data_cache/csv/`): 可选导出

## 数据流

```
长桥 API → data_fetcher.py → 清洗 → SQLite
                                      ↓
                              策略层读取 → 信号生成
```

## 关键设计决策

1. **前复权为默认**: 美股通常使用前复权 (Forward Adjust) 做回测
2. **UTC 统一存储**: 所有时间戳以 UTC 存入数据库，展示时转东部时间
3. **请求节流**: 长桥 API 限流 5 QPS，客户端内置 throttle
4. **失败重试**: API 调用最多重试 3 次，指数退避

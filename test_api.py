#!/usr/bin/env python3
"""
LongPort OpenAPI 连通性测试
测试内容：
1. 行情 API — 获取 AAPL 实时报价
2. 交易 API — 查询账户资金
3. 交易 API — 查询持仓
"""

import os
import sys
from dotenv import load_dotenv

# 加载 .env
load_dotenv()

print("=" * 60)
print("🔧 LongPort OpenAPI 连通性测试")
print("=" * 60)

# 检查环境变量
app_key = os.getenv("LONGBRIDGE_APP_KEY")
app_secret = os.getenv("LONGBRIDGE_APP_SECRET")
access_token = os.getenv("LONGBRIDGE_ACCESS_TOKEN")

if not all([app_key, app_secret, access_token]):
    print("❌ 缺少环境变量，请检查 .env 文件")
    sys.exit(1)

print(f"✅ App Key: {app_key[:8]}...{app_key[-4:]}")
print(f"✅ App Secret: {app_secret[:8]}...{app_secret[-4:]}")
print(f"✅ Access Token: {access_token[:20]}...（长度 {len(access_token)}）")
print()

from longport.openapi import Config, QuoteContext, TradeContext

# 创建配置
config = Config.from_env()

# ============ 测试 1：行情 API ============
print("-" * 60)
print("📈 测试 1：行情 API — 获取 AAPL.US 实时报价")
print("-" * 60)
try:
    quote_ctx = QuoteContext(config)
    resp = quote_ctx.quote(["AAPL.US"])
    if resp:
        q = resp[0]
        print(f"  股票代码: {q.symbol}")
        print(f"  最新价格: ${q.last_done}")
        print(f"  最高价:   ${q.high}")
        print(f"  最低价:   ${q.low}")
        print(f"  开盘价:   ${q.open}")
        print(f"  昨收价:   ${q.prev_close}")
        print(f"  成交量:   {q.volume}")
        print(f"  成交额:   ${q.turnover}")
        print(f"  ✅ 行情 API 测试通过！")
    else:
        print("  ⚠️ 返回数据为空")
except Exception as e:
    print(f"  ❌ 行情 API 测试失败: {e}")

print()

# ============ 测试 2：交易 API — 账户资金 ============
print("-" * 60)
print("💰 测试 2：交易 API — 查询账户资金")
print("-" * 60)
try:
    trade_ctx = TradeContext(config)
    account_balance = trade_ctx.account_balance()
    if account_balance:
        for bal in account_balance:
            print(f"  总资产:       {bal.total_cash} {bal.currency}")
            print(f"  可用现金:     {bal.cash_infos}")
            print(f"  最大融资金额: {bal.max_finance_amount}")
            # 尝试获取更多详情
            if hasattr(bal, 'net_assets'):
                print(f"  净资产:       {bal.net_assets}")
        print(f"  ✅ 账户资金查询通过！")
    else:
        print("  ⚠️ 返回数据为空（模拟账户可能无余额）")
except Exception as e:
    print(f"  ❌ 账户资金查询失败: {e}")

print()

# ============ 测试 3：交易 API — 持仓查询 ============
print("-" * 60)
print("📊 测试 3：交易 API — 查询当前持仓")
print("-" * 60)
try:
    positions = trade_ctx.stock_positions()
    if positions and positions.channels:
        for channel in positions.channels:
            print(f"  渠道: {channel.account_channel}")
            if channel.positions:
                for pos in channel.positions:
                    print(f"    {pos.symbol}: 数量={pos.quantity}, 可用={pos.available_quantity}, 成本={pos.cost_price}")
            else:
                print(f"    （暂无持仓）")
    else:
        print("  （暂无持仓，模拟账户初始状态）")
    print(f"  ✅ 持仓查询通过！")
except Exception as e:
    print(f"  ❌ 持仓查询失败: {e}")

print()

# ============ 测试 4：历史 K 线 ============
print("-" * 60)
print("📉 测试 4：行情 API — 获取 AAPL.US 历史 K 线（最近5天）")
print("-" * 60)
try:
    from longport.openapi import Period, AdjustType
    from datetime import date
    
    candlesticks = quote_ctx.candlesticks("AAPL.US", Period.Day, 5, AdjustType.ForwardAdjust)
    if candlesticks:
        print(f"  获取到 {len(candlesticks)} 根 K 线：")
        for k in candlesticks:
            print(f"    {k.timestamp.date()} | 开:{k.open} 高:{k.high} 低:{k.low} 收:{k.close} 量:{k.volume}")
        print(f"  ✅ 历史 K 线测试通过！")
    else:
        print("  ⚠️ 返回数据为空")
except Exception as e:
    print(f"  ❌ 历史 K 线测试失败: {e}")

print()
print("=" * 60)
print("🏁 测试全部完成")
print("=" * 60)

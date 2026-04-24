#!/usr/bin/env python3
"""
META 回测 - MA Cross 策略
初始资金: 800,000 HKD ≈ 102,564 USD (按 7.8 汇率)
数据源: data_cache/csv/META_US_1d.csv
"""

import os
import sys
import json
from datetime import datetime

import numpy as np
import pandas as pd

# ----------------------------------------------------------
# 1. 加载数据
# ----------------------------------------------------------
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
csv_path = os.path.join(project_root, "data_cache", "csv", "META_US_1d.csv")

df = pd.read_csv(csv_path)
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)

print(f"Loaded {len(df)} bars: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
print(f"Price range: ${df['close'].min():.2f} ~ ${df['close'].max():.2f}")

# ----------------------------------------------------------
# 2. 策略参数 & 风控参数
# ----------------------------------------------------------
INITIAL_CAPITAL_HKD = 800_000
HKD_TO_USD = 7.8
INITIAL_CAPITAL = INITIAL_CAPITAL_HKD / HKD_TO_USD  # ≈ $102,564

SHORT_MA = 5
LONG_MA = 20
SIGNAL_TYPE = "SMA"

# 手续费: 每股 $0.0049 + 平台费 $0.005, 最低 $0.99
COMMISSION_PER_SHARE = 0.0049 + 0.005
MIN_COMMISSION = 0.99
SLIPPAGE_PCT = 0.001  # 0.1%

# 风控
MAX_SINGLE_POSITION_PCT = 0.95  # 单票最大仓位 95%（满仓策略）
STOP_LOSS_PCT = 0.05   # 止损 -5%
TAKE_PROFIT_PCT = 0.15  # 止盈 +15%
TRAILING_STOP_PCT = 0.05  # 追踪止损 5%

# ----------------------------------------------------------
# 3. 计算信号
# ----------------------------------------------------------
if SIGNAL_TYPE == "EMA":
    df["ma_short"] = df["close"].ewm(span=SHORT_MA, adjust=False).mean()
    df["ma_long"] = df["close"].ewm(span=LONG_MA, adjust=False).mean()
else:
    df["ma_short"] = df["close"].rolling(window=SHORT_MA).mean()
    df["ma_long"] = df["close"].rolling(window=LONG_MA).mean()

# 信号: 金叉买, 死叉卖
df["signal"] = 0
for i in range(1, len(df)):
    prev_short = df.loc[i - 1, "ma_short"]
    prev_long = df.loc[i - 1, "ma_long"]
    curr_short = df.loc[i, "ma_short"]
    curr_long = df.loc[i, "ma_long"]
    
    if pd.isna(prev_short) or pd.isna(prev_long):
        continue
    
    # 金叉
    if prev_short <= prev_long and curr_short > curr_long:
        df.loc[i, "signal"] = 1  # BUY
    # 死叉
    elif prev_short >= prev_long and curr_short < curr_long:
        df.loc[i, "signal"] = -1  # SELL

# ----------------------------------------------------------
# 4. 回测引擎 (收盘信号 -> 次日开盘执行)
# ----------------------------------------------------------
cash = INITIAL_CAPITAL
position = 0  # 持仓股数
entry_price = 0.0
highest_since_entry = 0.0
trades = []
equity_curve = []
daily_records = []

for i in range(len(df)):
    row = df.iloc[i]
    current_price = row["close"]
    
    # 记录每日净值
    portfolio_value = cash + position * current_price
    equity_curve.append({"date": row["date"], "value": portfolio_value})
    
    # 风控检查（如果有持仓）
    if position > 0:
        highest_since_entry = max(highest_since_entry, current_price)
        pnl_pct = (current_price - entry_price) / entry_price
        
        # 止损
        if pnl_pct <= -STOP_LOSS_PCT:
            sell_price = current_price * (1 - SLIPPAGE_PCT)
            comm = max(position * COMMISSION_PER_SHARE, MIN_COMMISSION)
            proceeds = position * sell_price - comm
            pnl = proceeds - position * entry_price
            trades.append({
                "entry_date": entry_date_str,
                "exit_date": str(row["date"].date()),
                "entry_price": entry_price,
                "exit_price": sell_price,
                "shares": position,
                "pnl": pnl,
                "pnl_pct": (sell_price - entry_price) / entry_price,
                "reason": f"止损 ({pnl_pct:.1%})",
                "commission": comm,
            })
            cash += proceeds
            position = 0
            continue
        
        # 止盈
        if pnl_pct >= TAKE_PROFIT_PCT:
            sell_price = current_price * (1 - SLIPPAGE_PCT)
            comm = max(position * COMMISSION_PER_SHARE, MIN_COMMISSION)
            proceeds = position * sell_price - comm
            pnl = proceeds - position * entry_price
            trades.append({
                "entry_date": entry_date_str,
                "exit_date": str(row["date"].date()),
                "entry_price": entry_price,
                "exit_price": sell_price,
                "shares": position,
                "pnl": pnl,
                "pnl_pct": (sell_price - entry_price) / entry_price,
                "reason": f"止盈 ({pnl_pct:.1%})",
                "commission": comm,
            })
            cash += proceeds
            position = 0
            continue
        
        # 追踪止损
        trail_pct = (current_price - highest_since_entry) / highest_since_entry
        if trail_pct <= -TRAILING_STOP_PCT:
            sell_price = current_price * (1 - SLIPPAGE_PCT)
            comm = max(position * COMMISSION_PER_SHARE, MIN_COMMISSION)
            proceeds = position * sell_price - comm
            pnl = proceeds - position * entry_price
            trades.append({
                "entry_date": entry_date_str,
                "exit_date": str(row["date"].date()),
                "entry_price": entry_price,
                "exit_price": sell_price,
                "shares": position,
                "pnl": pnl,
                "pnl_pct": (sell_price - entry_price) / entry_price,
                "reason": f"追踪止损 (高点回撤{trail_pct:.1%})",
                "commission": comm,
            })
            cash += proceeds
            position = 0
            continue
    
    # 信号在当日收盘产生, 次日开盘执行
    # 这里简化: 用当日收盘价 + 滑点模拟次日开盘
    signal = row["signal"]
    
    if signal == 1 and position == 0:
        # 买入
        buy_price = current_price * (1 + SLIPPAGE_PCT)
        max_shares = int((cash * MAX_SINGLE_POSITION_PCT) / buy_price)
        if max_shares > 0:
            comm = max(max_shares * COMMISSION_PER_SHARE, MIN_COMMISSION)
            cost = max_shares * buy_price + comm
            if cost <= cash:
                cash -= cost
                position = max_shares
                entry_price = buy_price
                entry_date_str = str(row["date"].date())
                highest_since_entry = current_price
    
    elif signal == -1 and position > 0:
        # 卖出
        sell_price = current_price * (1 - SLIPPAGE_PCT)
        comm = max(position * COMMISSION_PER_SHARE, MIN_COMMISSION)
        proceeds = position * sell_price - comm
        pnl = proceeds - position * entry_price
        trades.append({
            "entry_date": entry_date_str,
            "exit_date": str(row["date"].date()),
            "entry_price": entry_price,
            "exit_price": sell_price,
            "shares": position,
            "pnl": pnl,
            "pnl_pct": (sell_price - entry_price) / entry_price,
            "reason": "死叉信号",
            "commission": comm,
        })
        cash += proceeds
        position = 0

# 如果回测结束时还有持仓，按最后收盘价平仓
if position > 0:
    sell_price = df.iloc[-1]["close"]
    comm = max(position * COMMISSION_PER_SHARE, MIN_COMMISSION)
    proceeds = position * sell_price - comm
    pnl = proceeds - position * entry_price
    trades.append({
        "entry_date": entry_date_str,
        "exit_date": str(df.iloc[-1]["date"].date()),
        "entry_price": entry_price,
        "exit_price": sell_price,
        "shares": position,
        "pnl": pnl,
        "pnl_pct": (sell_price - entry_price) / entry_price,
        "reason": "回测结束平仓",
        "commission": comm,
    })
    cash += proceeds
    position = 0

# ----------------------------------------------------------
# 5. 计算绩效指标
# ----------------------------------------------------------
eq = pd.DataFrame(equity_curve)
eq["date"] = pd.to_datetime(eq["date"])
eq = eq.set_index("date")

final_value = cash
total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL
days = (eq.index[-1] - eq.index[0]).days
annual_return = (1 + total_return) ** (365.0 / days) - 1 if days > 0 else 0

# 最大回撤
rolling_max = eq["value"].cummax()
drawdown = (eq["value"] - rolling_max) / rolling_max
max_drawdown = abs(drawdown.min())

# 夏普比率
daily_returns = eq["value"].pct_change().dropna()
risk_free_daily = (1 + 0.05) ** (1.0 / 252) - 1
excess = daily_returns - risk_free_daily
sharpe = np.sqrt(252) * excess.mean() / excess.std() if excess.std() > 0 else 0

# Sortino
downside = excess[excess < 0]
sortino = np.sqrt(252) * excess.mean() / downside.std() if len(downside) > 0 and downside.std() > 0 else 0

# 交易统计
winning = [t for t in trades if t["pnl"] > 0]
losing = [t for t in trades if t["pnl"] <= 0]
win_rate = len(winning) / len(trades) if trades else 0
avg_win = np.mean([t["pnl"] for t in winning]) if winning else 0
avg_loss = abs(np.mean([t["pnl"] for t in losing])) if losing else 1
profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

# 持仓天数
holding_days = []
for t in trades:
    try:
        d1 = pd.to_datetime(t["entry_date"])
        d2 = pd.to_datetime(t["exit_date"])
        holding_days.append((d2 - d1).days)
    except:
        pass

# 买入持有基准
buy_hold_return = (df["close"].iloc[-1] - df["close"].iloc[LONG_MA]) / df["close"].iloc[LONG_MA]

metrics = {
    "initial_capital_hkd": INITIAL_CAPITAL_HKD,
    "initial_capital_usd": round(INITIAL_CAPITAL, 2),
    "final_value_usd": round(final_value, 2),
    "total_return": total_return,
    "annual_return": annual_return,
    "max_drawdown": max_drawdown,
    "sharpe_ratio": sharpe,
    "sortino_ratio": sortino,
    "trade_count": len(trades),
    "win_count": len(winning),
    "loss_count": len(losing),
    "win_rate": win_rate,
    "profit_loss_ratio": profit_loss_ratio,
    "avg_holding_days": np.mean(holding_days) if holding_days else 0,
    "max_win": max(t["pnl"] for t in trades) if trades else 0,
    "max_loss": min(t["pnl"] for t in trades) if trades else 0,
    "total_commission": sum(t["commission"] for t in trades),
    "buy_hold_return": buy_hold_return,
    "total_days": days,
}

# ----------------------------------------------------------
# 6. 打印结果
# ----------------------------------------------------------
print("\n" + "=" * 60)
print("  META 回测报告 - MA Cross (5/20)")
print("=" * 60)
print(f"  初始资金(HKD):    HK${INITIAL_CAPITAL_HKD:>12,.0f}")
print(f"  初始资金(USD):    ${INITIAL_CAPITAL:>13,.2f}")
print(f"  最终价值(USD):    ${final_value:>13,.2f}")
print(f"  总收益率:         {total_return:>13.2%}")
print(f"  年化收益率:       {annual_return:>13.2%}")
print(f"  最大回撤:         {max_drawdown:>13.2%}")
print(f"  夏普比率:         {sharpe:>13.2f}")
print(f"  Sortino比率:      {sortino:>13.2f}")
print("-" * 60)
print(f"  总交易次数:       {len(trades):>13d}")
print(f"  胜率:             {win_rate:>13.2%}")
print(f"  盈亏比:           {profit_loss_ratio:>13.2f}")
print(f"  平均持仓天数:     {np.mean(holding_days) if holding_days else 0:>13.1f}")
print(f"  最大单笔盈利:     ${metrics['max_win']:>12,.2f}")
print(f"  最大单笔亏损:     ${metrics['max_loss']:>12,.2f}")
print(f"  总手续费:         ${metrics['total_commission']:>12,.2f}")
print("-" * 60)
print(f"  买入持有收益率:   {buy_hold_return:>13.2%}")
print(f"  策略 vs 买持:     {total_return - buy_hold_return:>+13.2%}")
print("=" * 60)

print("\n交易明细:")
print(f"{'#':>3} {'入场日期':>12} {'出场日期':>12} {'入场价':>10} {'出场价':>10} {'股数':>6} {'盈亏($)':>12} {'盈亏%':>8} {'原因'}")
print("-" * 95)
for i, t in enumerate(trades, 1):
    print(f"{i:>3} {t['entry_date']:>12} {t['exit_date']:>12} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} "
          f"{t['shares']:>6} {t['pnl']:>12,.2f} {t['pnl_pct']:>8.2%} {t['reason']}")

# ----------------------------------------------------------
# 7. 生成 HTML 报告
# ----------------------------------------------------------
output_dir = os.path.join(project_root, "output")
os.makedirs(output_dir, exist_ok=True)

# 准备图表数据
dates_json = json.dumps([str(d.date()) for d in eq.index])
equity_json = json.dumps([round(v, 2) for v in eq["value"].values])
drawdown_json = json.dumps([round(v * 100, 2) for v in drawdown.values])

# K线数据
kline_dates = json.dumps([str(d.date()) for d in df["date"]])
kline_close = json.dumps([round(v, 2) for v in df["close"].values])
kline_ma_short = json.dumps([round(v, 2) if not pd.isna(v) else None for v in df["ma_short"].values])
kline_ma_long = json.dumps([round(v, 2) if not pd.isna(v) else None for v in df["ma_long"].values])

# 买卖点
buy_points = []
sell_points = []
for t in trades:
    buy_points.append({"date": t["entry_date"], "price": round(t["entry_price"], 2)})
    sell_points.append({"date": t["exit_date"], "price": round(t["exit_price"], 2), "reason": t["reason"]})
buy_json = json.dumps(buy_points)
sell_json = json.dumps(sell_points)

# 交易表格
trades_html = ""
for i, t in enumerate(trades, 1):
    color = "#e74c3c" if t["pnl"] < 0 else "#27ae60"
    trades_html += f"""
    <tr>
        <td>{i}</td>
        <td>{t['entry_date']}</td>
        <td>{t['exit_date']}</td>
        <td>${t['entry_price']:.2f}</td>
        <td>${t['exit_price']:.2f}</td>
        <td>{t['shares']}</td>
        <td style="color:{color};font-weight:bold">${t['pnl']:,.2f}</td>
        <td style="color:{color}">{t['pnl_pct']:.2%}</td>
        <td>{t['reason']}</td>
    </tr>"""

# 绩效颜色
ret_color = "#e74c3c" if total_return < 0 else "#27ae60"
bh_color = "#e74c3c" if buy_hold_return < 0 else "#27ae60"
alpha_color = "#e74c3c" if (total_return - buy_hold_return) < 0 else "#27ae60"

html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>META 回测报告 - MA Cross (5/20)</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .header h1 {{ color: #58a6ff; font-size: 28px; margin-bottom: 8px; }}
        .header .subtitle {{ color: #8b949e; font-size: 14px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 25px; }}
        .metric-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 18px; text-align: center; }}
        .metric-card .label {{ color: #8b949e; font-size: 12px; margin-bottom: 6px; text-transform: uppercase; }}
        .metric-card .value {{ font-size: 24px; font-weight: bold; }}
        .metric-card .sub {{ color: #8b949e; font-size: 11px; margin-top: 4px; }}
        .chart-container {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; margin-bottom: 25px; }}
        .chart-title {{ color: #c9d1d9; font-size: 16px; font-weight: 600; margin-bottom: 15px; }}
        .chart {{ width: 100%; height: 400px; }}
        .trades-table {{ width: 100%; border-collapse: collapse; }}
        .trades-table th {{ background: #21262d; color: #8b949e; padding: 10px; text-align: left; font-size: 12px; text-transform: uppercase; border-bottom: 1px solid #30363d; }}
        .trades-table td {{ padding: 10px; border-bottom: 1px solid #21262d; font-size: 13px; }}
        .trades-table tr:hover {{ background: #1c2128; }}
        .section-title {{ color: #58a6ff; font-size: 18px; font-weight: 600; margin: 25px 0 15px; }}
        .green {{ color: #27ae60; }}
        .red {{ color: #e74c3c; }}
        .params-bar {{ display: flex; gap: 20px; justify-content: center; margin: 15px 0; flex-wrap: wrap; }}
        .param {{ background: #21262d; padding: 6px 14px; border-radius: 20px; font-size: 12px; color: #8b949e; }}
        .param span {{ color: #58a6ff; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 META 回测报告</h1>
        <div class="subtitle">MA Cross Strategy (SMA {SHORT_MA}/{LONG_MA}) | {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()} | {len(df)}个交易日</div>
        <div class="params-bar">
            <div class="param">初始资金 <span>HK${INITIAL_CAPITAL_HKD:,.0f}</span> (≈<span>${INITIAL_CAPITAL:,.0f}</span>)</div>
            <div class="param">手续费 <span>${COMMISSION_PER_SHARE}/股</span></div>
            <div class="param">滑点 <span>{SLIPPAGE_PCT:.1%}</span></div>
            <div class="param">止损 <span>{STOP_LOSS_PCT:.0%}</span></div>
            <div class="param">止盈 <span>{TAKE_PROFIT_PCT:.0%}</span></div>
            <div class="param">追踪止损 <span>{TRAILING_STOP_PCT:.0%}</span></div>
        </div>
    </div>

    <div class="metrics-grid">
        <div class="metric-card">
            <div class="label">总收益率</div>
            <div class="value" style="color:{ret_color}">{total_return:.2%}</div>
            <div class="sub">${INITIAL_CAPITAL:,.0f} → ${final_value:,.0f}</div>
        </div>
        <div class="metric-card">
            <div class="label">年化收益率</div>
            <div class="value" style="color:{ret_color}">{annual_return:.2%}</div>
            <div class="sub">{days}个自然日</div>
        </div>
        <div class="metric-card">
            <div class="label">最大回撤</div>
            <div class="value red">{max_drawdown:.2%}</div>
            <div class="sub">风控阈值 10%</div>
        </div>
        <div class="metric-card">
            <div class="label">夏普比率</div>
            <div class="value" style="color:{'#27ae60' if sharpe > 1 else '#e5c07b' if sharpe > 0 else '#e74c3c'}">{sharpe:.2f}</div>
            <div class="sub">无风险利率 5%</div>
        </div>
        <div class="metric-card">
            <div class="label">总交易次数</div>
            <div class="value" style="color:#58a6ff">{len(trades)}</div>
            <div class="sub">盈 {len(winning)} / 亏 {len(losing)}</div>
        </div>
        <div class="metric-card">
            <div class="label">胜率</div>
            <div class="value" style="color:{'#27ae60' if win_rate > 0.5 else '#e74c3c'}">{win_rate:.1%}</div>
            <div class="sub">盈亏比 {profit_loss_ratio:.2f}</div>
        </div>
        <div class="metric-card">
            <div class="label">买入持有收益</div>
            <div class="value" style="color:{bh_color}">{buy_hold_return:.2%}</div>
            <div class="sub">同期 META 涨跌幅</div>
        </div>
        <div class="metric-card">
            <div class="label">超额收益(Alpha)</div>
            <div class="value" style="color:{alpha_color}">{total_return - buy_hold_return:+.2%}</div>
            <div class="sub">策略 vs 买入持有</div>
        </div>
    </div>

    <div class="chart-container">
        <div class="chart-title">📈 净值曲线 & 回撤</div>
        <div id="equity-chart" class="chart"></div>
    </div>

    <div class="chart-container">
        <div class="chart-title">📉 K线 & 买卖点</div>
        <div id="kline-chart" class="chart" style="height:450px"></div>
    </div>

    <div class="chart-container">
        <div class="chart-title">💰 每笔交易盈亏</div>
        <div id="pnl-chart" class="chart" style="height:300px"></div>
    </div>

    <h2 class="section-title">📋 交易明细</h2>
    <div class="chart-container" style="padding:0;overflow-x:auto">
        <table class="trades-table">
            <thead>
                <tr>
                    <th>#</th><th>入场日期</th><th>出场日期</th><th>入场价</th><th>出场价</th>
                    <th>股数</th><th>盈亏($)</th><th>盈亏%</th><th>出场原因</th>
                </tr>
            </thead>
            <tbody>{trades_html}</tbody>
        </table>
    </div>

    <script>
        // 净值曲线
        var equityChart = echarts.init(document.getElementById('equity-chart'));
        equityChart.setOption({{
            tooltip: {{ trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: {{ color: '#c9d1d9' }} }},
            legend: {{ data: ['净值', '回撤%'], textStyle: {{ color: '#8b949e' }}, top: 0 }},
            grid: {{ left: 80, right: 60, top: 40, bottom: 30 }},
            xAxis: {{ type: 'category', data: {dates_json}, axisLabel: {{ color: '#8b949e' }}, axisLine: {{ lineStyle: {{ color: '#30363d' }} }} }},
            yAxis: [
                {{ type: 'value', name: 'USD', axisLabel: {{ color: '#8b949e', formatter: '${'{value}'}' }}, splitLine: {{ lineStyle: {{ color: '#21262d' }} }} }},
                {{ type: 'value', name: '回撤%', axisLabel: {{ color: '#8b949e', formatter: '{'{value}'}%' }}, splitLine: {{ show: false }} }}
            ],
            series: [
                {{ name: '净值', type: 'line', data: {equity_json}, smooth: true, lineStyle: {{ color: '#58a6ff', width: 2 }}, itemStyle: {{ color: '#58a6ff' }}, showSymbol: false, areaStyle: {{ color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{{offset:0, color:'rgba(88,166,255,0.3)'}}, {{offset:1, color:'rgba(88,166,255,0.02)'}}]) }} }},
                {{ name: '回撤%', type: 'line', yAxisIndex: 1, data: {drawdown_json}, smooth: true, lineStyle: {{ color: '#e74c3c', width: 1 }}, itemStyle: {{ color: '#e74c3c' }}, showSymbol: false, areaStyle: {{ color: 'rgba(231,76,60,0.1)' }} }}
            ],
            dataZoom: [{{ type: 'inside', start: 0, end: 100 }}]
        }});

        // K线图
        var klineChart = echarts.init(document.getElementById('kline-chart'));
        var buyPoints = {buy_json};
        var sellPoints = {sell_json};
        var klineDates = {kline_dates};
        
        var buyMarkData = buyPoints.map(function(p) {{ return {{ coord: [p.date, p.price], value: 'B' }}; }});
        var sellMarkData = sellPoints.map(function(p) {{ return {{ coord: [p.date, p.price], value: p.reason }}; }});
        
        klineChart.setOption({{
            tooltip: {{ trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: {{ color: '#c9d1d9' }} }},
            legend: {{ data: ['收盘价', 'MA{SHORT_MA}', 'MA{LONG_MA}'], textStyle: {{ color: '#8b949e' }}, top: 0 }},
            grid: {{ left: 80, right: 60, top: 40, bottom: 30 }},
            xAxis: {{ type: 'category', data: klineDates, axisLabel: {{ color: '#8b949e' }}, axisLine: {{ lineStyle: {{ color: '#30363d' }} }} }},
            yAxis: {{ type: 'value', name: 'USD', axisLabel: {{ color: '#8b949e', formatter: '${'{value}'}' }}, splitLine: {{ lineStyle: {{ color: '#21262d' }} }} }},
            series: [
                {{ name: '收盘价', type: 'line', data: {kline_close}, smooth: false, lineStyle: {{ color: '#c9d1d9', width: 1.5 }}, showSymbol: false,
                  markPoint: {{
                    data: buyMarkData.map(function(p) {{ return {{ coord: p.coord, symbol: 'triangle', symbolSize: 12, itemStyle: {{ color: '#27ae60' }}, label: {{ show: false }} }}; }}).concat(
                      sellMarkData.map(function(p) {{ return {{ coord: p.coord, symbol: 'pin', symbolSize: 14, itemStyle: {{ color: '#e74c3c' }}, label: {{ show: false }} }}; }})
                    )
                  }}
                }},
                {{ name: 'MA{SHORT_MA}', type: 'line', data: {kline_ma_short}, smooth: true, lineStyle: {{ color: '#f0c040', width: 1 }}, showSymbol: false }},
                {{ name: 'MA{LONG_MA}', type: 'line', data: {kline_ma_long}, smooth: true, lineStyle: {{ color: '#e06040', width: 1 }}, showSymbol: false }}
            ],
            dataZoom: [{{ type: 'inside', start: 0, end: 100 }}]
        }});

        // 盈亏柱状图
        var pnlChart = echarts.init(document.getElementById('pnl-chart'));
        var tradeData = {json.dumps([{"label": f"#{i+1}", "pnl": round(t['pnl'], 2)} for i, t in enumerate(trades)])};
        pnlChart.setOption({{
            tooltip: {{ trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: {{ color: '#c9d1d9' }}, formatter: function(p) {{ return p[0].name + ': $' + p[0].value.toLocaleString(); }} }},
            grid: {{ left: 80, right: 20, top: 10, bottom: 30 }},
            xAxis: {{ type: 'category', data: tradeData.map(function(d) {{ return d.label; }}), axisLabel: {{ color: '#8b949e' }} }},
            yAxis: {{ type: 'value', axisLabel: {{ color: '#8b949e', formatter: '${'{value}'}' }}, splitLine: {{ lineStyle: {{ color: '#21262d' }} }} }},
            series: [{{
                type: 'bar', data: tradeData.map(function(d) {{ return {{ value: d.pnl, itemStyle: {{ color: d.pnl >= 0 ? '#27ae60' : '#e74c3c' }} }}; }})
            }}]
        }});

        window.addEventListener('resize', function() {{
            equityChart.resize();
            klineChart.resize();
            pnlChart.resize();
        }});
    </script>
</body>
</html>"""

html_path = os.path.join(output_dir, "META_backtest_report.html")
with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\n✅ HTML 报告已生成: {html_path}")

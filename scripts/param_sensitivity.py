"""
参数敏感性分析 (Parameter Sensitivity Analysis)
========================================
对 MA Cross / RSI / Momentum 三种策略做网格搜索，
输出热力图和稳定区间分析，判断策略是否过拟合。
"""

import sys, os, warnings, itertools, json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── project root on path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.ma_cross_strategy import MACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy
from src.strategy.momentum_strategy import MomentumStrategy
from src.backtest.engine import BacktestEngine

# ================================================================
# 配置：网格搜索参数范围
# ================================================================

MA_GRID = {
    "short_period": [3, 5, 8, 10, 15, 20],
    "long_period":  [20, 30, 40, 50, 60, 80, 100, 120],
}

RSI_GRID = {
    "period":     [7, 10, 14, 20, 28],
    "oversold":   [20, 25, 30, 35, 40],
    "overbought": [60, 65, 70, 75, 80],
}

MOMENTUM_GRID = {
    "lookback_period": [5, 10, 15, 20, 30, 40, 60],
    "buy_threshold":   [0.02, 0.03, 0.05, 0.08, 0.10, 0.15],
}

INITIAL_CAPITAL = 102564.0   # HK$800k ≈ US$102,564

# ================================================================
# 工具函数
# ================================================================

def load_meta_data() -> pd.DataFrame:
    csv_path = PROJECT_ROOT / "data_cache" / "csv" / "META_US_1d.csv"
    df = pd.read_csv(csv_path, parse_dates=["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def run_single_backtest(strategy_cls, config: dict, data: pd.DataFrame) -> dict:
    """跑一次回测，返回核心指标；出错返回 None 指标。"""
    try:
        strat = strategy_cls()
        strat.init(config)
        engine = BacktestEngine({"backtest": {
            "initial_capital": INITIAL_CAPITAL,
            "commission": {"type": "per_share", "rate": 0.0049, "platform_fee": 0.005},
            "slippage": {"type": "percentage", "value": 0.001},
        }})
        result = engine.run(strat, data, symbol="META.US", initial_capital=INITIAL_CAPITAL)
        m = result["metrics"]
        return {
            "total_return":  m.get("total_return", 0),
            "annual_return": m.get("annual_return", 0),
            "max_drawdown":  m.get("max_drawdown", 0),
            "sharpe":        m.get("sharpe_ratio", 0) or 0,
            "win_rate":      m.get("win_rate", 0),
            "trade_count":   m.get("trade_count", 0),
        }
    except Exception as e:
        return {
            "total_return": None, "annual_return": None,
            "max_drawdown": None, "sharpe": None,
            "win_rate": None, "trade_count": None,
            "error": str(e),
        }


# ================================================================
# 网格搜索
# ================================================================

def grid_search_ma(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    combos = list(itertools.product(MA_GRID["short_period"], MA_GRID["long_period"]))
    total = len(combos)
    for i, (sp, lp) in enumerate(combos, 1):
        if sp >= lp:          # 短周期必须 < 长周期
            continue
        print(f"  MA Cross [{i}/{total}] short={sp}, long={lp} ...", end="", flush=True)
        cfg = {"short_period": sp, "long_period": lp, "signal_type": "SMA"}
        m = run_single_backtest(MACrossStrategy, cfg, data)
        m["short_period"] = sp
        m["long_period"] = lp
        rows.append(m)
        ret = m["total_return"]
        print(f"  return={ret:.2%}" if ret is not None else "  FAIL")
    return pd.DataFrame(rows)


def grid_search_rsi(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    combos = list(itertools.product(
        RSI_GRID["period"], RSI_GRID["oversold"], RSI_GRID["overbought"]
    ))
    total = len(combos)
    for i, (period, os_val, ob_val) in enumerate(combos, 1):
        if os_val >= ob_val:
            continue
        print(f"  RSI [{i}/{total}] period={period}, os={os_val}, ob={ob_val} ...", end="", flush=True)
        cfg = {"period": period, "oversold": os_val, "overbought": ob_val}
        m = run_single_backtest(RSIStrategy, cfg, data)
        m["period"] = period
        m["oversold"] = os_val
        m["overbought"] = ob_val
        rows.append(m)
        ret = m["total_return"]
        print(f"  return={ret:.2%}" if ret is not None else "  FAIL")
    return pd.DataFrame(rows)


def grid_search_momentum(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    combos = list(itertools.product(
        MOMENTUM_GRID["lookback_period"], MOMENTUM_GRID["buy_threshold"]
    ))
    total = len(combos)
    for i, (lb, bt) in enumerate(combos, 1):
        print(f"  Momentum [{i}/{total}] lookback={lb}, buy_thresh={bt:.0%} ...", end="", flush=True)
        cfg = {"lookback_period": lb, "buy_threshold": bt, "sell_threshold": -bt * 0.6}
        m = run_single_backtest(MomentumStrategy, cfg, data)
        m["lookback_period"] = lb
        m["buy_threshold"] = bt
        rows.append(m)
        ret = m["total_return"]
        print(f"  return={ret:.2%}" if ret is not None else "  FAIL")
    return pd.DataFrame(rows)


# ================================================================
# 稳定性分析
# ================================================================

def stability_analysis(df: pd.DataFrame, param_cols: list, metric: str = "sharpe") -> dict:
    """分析参数稳定性：计算每个参数维度的方差、找稳定区间。"""
    valid = df.dropna(subset=[metric]).copy()
    if valid.empty:
        return {"stable_zone": "无有效数据", "sensitivity": "N/A"}

    results = {}
    # 1. 每个参数维度的敏感性
    for col in param_cols:
        grouped = valid.groupby(col)[metric].agg(["mean", "std", "count"])
        grouped["cv"] = grouped["std"] / grouped["mean"].abs().clip(lower=0.001)
        results[f"{col}_sensitivity"] = grouped.to_dict("index")

    # 2. 找稳定区间（连续参数值中，指标变化 < 20%的区域）
    best_val = valid[metric].max()
    threshold = best_val * 0.8 if best_val > 0 else best_val * 1.2
    stable = valid[valid[metric] >= threshold] if best_val > 0 else valid[valid[metric] <= threshold]
    stable_zones = {}
    for col in param_cols:
        if not stable.empty:
            stable_zones[col] = {
                "min": stable[col].min(),
                "max": stable[col].max(),
                "values": sorted(stable[col].unique().tolist()),
            }
    results["stable_zones"] = stable_zones

    # 3. 过拟合判断
    top_10pct = valid.nlargest(max(1, len(valid) // 10), metric)
    bottom_50pct = valid.nsmallest(max(1, len(valid) // 2), metric)
    spread = top_10pct[metric].mean() - bottom_50pct[metric].mean()
    median_val = valid[metric].median()
    overall_std = valid[metric].std()
    results["overfit_analysis"] = {
        "best": float(valid[metric].max()),
        "worst": float(valid[metric].min()),
        "median": float(median_val),
        "std": float(overall_std),
        "top_10pct_mean": float(top_10pct[metric].mean()),
        "bottom_50pct_mean": float(bottom_50pct[metric].mean()),
        "spread": float(spread),
        "is_likely_overfit": bool(spread > 2 * overall_std),
    }
    return results


# ================================================================
# HTML 报告生成
# ================================================================

def generate_html_report(
    ma_df: pd.DataFrame, rsi_df: pd.DataFrame, mom_df: pd.DataFrame,
    ma_stab: dict, rsi_stab: dict, mom_stab: dict,
    buy_hold_return: float,
) -> str:
    """生成完整的参数敏感性分析 HTML 报告。"""

    # ── MA 热力图数据 ──
    ma_valid = ma_df.dropna(subset=["sharpe"])
    ma_pivot_sharpe = ma_valid.pivot_table(index="short_period", columns="long_period", values="sharpe")
    ma_pivot_return = ma_valid.pivot_table(index="short_period", columns="long_period", values="total_return")
    ma_pivot_dd     = ma_valid.pivot_table(index="short_period", columns="long_period", values="max_drawdown")
    ma_pivot_trades = ma_valid.pivot_table(index="short_period", columns="long_period", values="trade_count")

    # ── RSI：period × (oversold,overbought) 组合标签 ──
    rsi_valid = rsi_df.dropna(subset=["sharpe"])
    rsi_valid["os_ob"] = rsi_valid.apply(lambda r: f"{int(r['oversold'])}/{int(r['overbought'])}", axis=1)
    rsi_pivot_sharpe = rsi_valid.pivot_table(index="period", columns="os_ob", values="sharpe")
    rsi_pivot_return = rsi_valid.pivot_table(index="period", columns="os_ob", values="total_return")

    # ── Momentum 热力图 ──
    mom_valid = mom_df.dropna(subset=["sharpe"])
    mom_pivot_sharpe = mom_valid.pivot_table(index="lookback_period", columns="buy_threshold", values="sharpe")
    mom_pivot_return = mom_valid.pivot_table(index="lookback_period", columns="buy_threshold", values="total_return")

    def make_heatmap_table(pivot: pd.DataFrame, fmt: str = ".2f", color_scheme: str = "sharpe") -> str:
        """将 pivot DataFrame 转成带颜色的 HTML 表格。"""
        if pivot.empty:
            return "<p>无数据</p>"
        vals = pivot.values.flatten()
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            return "<p>无有效数据</p>"
        vmin, vmax = vals.min(), vals.max()

        html = '<table class="heatmap"><thead><tr><th></th>'
        for c in pivot.columns:
            html += f"<th>{c}</th>"
        html += "</tr></thead><tbody>"

        for idx in pivot.index:
            html += f"<tr><td class='row-header'>{idx}</td>"
            for c in pivot.columns:
                v = pivot.loc[idx, c]
                if pd.isna(v):
                    html += "<td class='na'>—</td>"
                else:
                    # 归一化到 0-1
                    if vmax - vmin > 0:
                        norm = (v - vmin) / (vmax - vmin)
                    else:
                        norm = 0.5

                    if color_scheme == "sharpe":
                        # 红绿色谱：高=绿(好)，低=红(差)
                        if v >= 0:
                            r = int(255 * (1 - norm))
                            g = int(180 + 75 * norm)
                            b = int(150 * (1 - norm))
                        else:
                            r = int(200 + 55 * (1 - norm))
                            g = int(100 * norm)
                            b = int(80 * norm)
                    elif color_scheme == "drawdown":
                        # 反转：低回撤=绿，高回撤=红
                        r = int(255 * norm)
                        g = int(200 * (1 - norm))
                        b = 100
                    else:
                        # 通用蓝色色谱
                        r = int(255 * (1 - norm))
                        g = int(200 * (1 - norm) + 55)
                        b = 255

                    color = f"rgb({r},{g},{b})"
                    text_color = "#000" if norm < 0.7 else "#fff"
                    formatted = f"{v:{fmt}}" if "%" not in fmt else f"{v:.1%}"
                    html += f'<td style="background:{color};color:{text_color}">{formatted}</td>'
            html += "</tr>"
        html += "</tbody></table>"
        return html

    def format_pct(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v:.2%}"

    def make_stability_summary(stab: dict, name: str) -> str:
        oa = stab.get("overfit_analysis", {})
        zones = stab.get("stable_zones", {})
        html = f'<div class="stab-card"><h4>📊 {name}</h4>'
        html += '<table class="summary"><tbody>'
        html += f'<tr><td>最佳 Sharpe</td><td>{oa.get("best", 0):.3f}</td></tr>'
        html += f'<tr><td>最差 Sharpe</td><td>{oa.get("worst", 0):.3f}</td></tr>'
        html += f'<tr><td>中位数</td><td>{oa.get("median", 0):.3f}</td></tr>'
        html += f'<tr><td>标准差</td><td>{oa.get("std", 0):.3f}</td></tr>'
        html += f'<tr><td>Top10% 均值</td><td>{oa.get("top_10pct_mean", 0):.3f}</td></tr>'
        html += f'<tr><td>Bottom50% 均值</td><td>{oa.get("bottom_50pct_mean", 0):.3f}</td></tr>'
        html += f'<tr><td>极差 (Spread)</td><td>{oa.get("spread", 0):.3f}</td></tr>'

        overfit = oa.get("is_likely_overfit", False)
        label = '🔴 可能过拟合' if overfit else '🟢 参数较稳定'
        color = '#e74c3c' if overfit else '#27ae60'
        html += f'<tr><td>过拟合判断</td><td style="color:{color};font-weight:bold">{label}</td></tr>'
        html += '</tbody></table>'

        if zones:
            html += '<p><strong>稳定区间（Sharpe 在最优 80% 以上）</strong></p><ul>'
            for param, info in zones.items():
                html += f'<li><code>{param}</code>: {info["values"]}</li>'
            html += '</ul>'

        html += '</div>'
        return html

    # ── 排行榜 ──
    def make_leaderboard(df: pd.DataFrame, param_cols: list, top_n: int = 10) -> str:
        valid = df.dropna(subset=["sharpe"]).copy()
        valid.sort_values("sharpe", ascending=False, inplace=True)
        top = valid.head(top_n)
        html = '<table class="leaderboard"><thead><tr><th>#</th>'
        for c in param_cols:
            html += f'<th>{c}</th>'
        html += '<th>收益率</th><th>Sharpe</th><th>最大回撤</th><th>胜率</th><th>交易数</th></tr></thead><tbody>'
        for i, (_, row) in enumerate(top.iterrows(), 1):
            html += f'<tr><td>{i}</td>'
            for c in param_cols:
                html += f'<td>{row[c]}</td>'
            html += f'<td>{row["total_return"]:.2%}</td>'
            html += f'<td>{row["sharpe"]:.3f}</td>'
            html += f'<td>{row["max_drawdown"]:.2%}</td>'
            html += f'<td>{row["win_rate"]:.1%}</td>'
            html += f'<td>{int(row["trade_count"])}</td>'
            html += '</tr>'
        html += '</tbody></table>'
        return html

    # ── 最终 HTML ──
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>参数敏感性分析报告 — META.US</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 24px; }}
  h1 {{ text-align: center; font-size: 28px; margin-bottom: 8px; color: #fff; }}
  h2 {{ font-size: 22px; margin: 32px 0 16px; color: #60a5fa; border-bottom: 2px solid #1e3a5f; padding-bottom: 8px; }}
  h3 {{ font-size: 18px; margin: 24px 0 12px; color: #93c5fd; }}
  h4 {{ font-size: 16px; margin: 12px 0 8px; color: #a78bfa; }}
  .subtitle {{ text-align: center; color: #888; margin-bottom: 24px; font-size: 14px; }}
  .meta-bar {{ display: flex; justify-content: center; gap: 32px; margin: 16px 0 32px; flex-wrap: wrap; }}
  .meta-item {{ background: #1a1a2e; padding: 12px 20px; border-radius: 8px; text-align: center; min-width: 140px; }}
  .meta-item .label {{ font-size: 12px; color: #888; }}
  .meta-item .value {{ font-size: 20px; font-weight: bold; margin-top: 4px; }}
  .meta-item .value.green {{ color: #22c55e; }}
  .meta-item .value.red {{ color: #ef4444; }}
  .meta-item .value.blue {{ color: #60a5fa; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 16px 0; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{ background: #141422; border: 1px solid #2a2a4a; border-radius: 10px; padding: 20px; overflow-x: auto; }}
  table.heatmap {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  table.heatmap th, table.heatmap td {{ padding: 8px 10px; text-align: center; border: 1px solid #333; }}
  table.heatmap th {{ background: #1e1e3e; color: #93c5fd; }}
  td.row-header {{ background: #1e1e3e; color: #93c5fd; font-weight: bold; }}
  td.na {{ color: #555; }}
  table.summary {{ width: 100%; font-size: 14px; margin: 8px 0; }}
  table.summary td {{ padding: 6px 10px; border-bottom: 1px solid #222; }}
  table.summary td:first-child {{ color: #888; width: 45%; }}
  table.leaderboard {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0; }}
  table.leaderboard th {{ background: #1e1e3e; color: #60a5fa; padding: 8px; border: 1px solid #333; }}
  table.leaderboard td {{ padding: 8px; border: 1px solid #333; text-align: center; }}
  table.leaderboard tr:nth-child(even) {{ background: #1a1a2e; }}
  table.leaderboard tr:first-child td {{ background: #1a3a1a; color: #22c55e; font-weight: bold; }}
  .stab-card {{ background: #141422; border: 1px solid #2a2a4a; border-radius: 10px; padding: 20px; margin: 12px 0; }}
  .stab-card ul {{ margin: 8px 0 0 20px; font-size: 14px; }}
  .stab-card li {{ margin: 4px 0; }}
  .verdict {{ background: #1a1a2e; border: 2px solid #3b82f6; border-radius: 12px; padding: 24px; margin: 32px 0; }}
  .verdict h3 {{ color: #fbbf24; margin-bottom: 12px; }}
  .verdict ul {{ margin: 12px 0 0 20px; line-height: 1.8; }}
  .verdict li {{ margin: 4px 0; }}
  code {{ background: #2a2a4a; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  .footer {{ text-align: center; color: #555; margin-top: 40px; font-size: 12px; }}
</style>
</head>
<body>

<h1>🔬 参数敏感性分析报告</h1>
<p class="subtitle">META.US | 回测区间: 2025-04 ~ 2026-04 | 生成时间: {now}</p>

<div class="meta-bar">
  <div class="meta-item">
    <div class="label">标的</div>
    <div class="value blue">META.US</div>
  </div>
  <div class="meta-item">
    <div class="label">初始资金</div>
    <div class="value">$102,564</div>
  </div>
  <div class="meta-item">
    <div class="label">买入持有收益</div>
    <div class="value {'green' if buy_hold_return >= 0 else 'red'}">{buy_hold_return:.2%}</div>
  </div>
  <div class="meta-item">
    <div class="label">MA 参数组合</div>
    <div class="value blue">{len(ma_df)} 组</div>
  </div>
  <div class="meta-item">
    <div class="label">RSI 参数组合</div>
    <div class="value blue">{len(rsi_df)} 组</div>
  </div>
  <div class="meta-item">
    <div class="label">Momentum 参数组合</div>
    <div class="value blue">{len(mom_df)} 组</div>
  </div>
</div>

<!-- ═══════════════ MA Cross ═══════════════ -->
<h2>1️⃣ MA Cross 均线交叉策略</h2>
<p>参数空间: short_period × long_period（{len(MA_GRID["short_period"])} × {len(MA_GRID["long_period"])}）</p>

<h3>Sharpe Ratio 热力图</h3>
<p style="font-size:13px;color:#888">行=短周期，列=长周期 | 绿色=好，红色=差</p>
<div class="card">{make_heatmap_table(ma_pivot_sharpe, ".3f", "sharpe")}</div>

<div class="grid">
  <div>
    <h3>总收益率热力图</h3>
    <div class="card">{make_heatmap_table(ma_pivot_return, ".2%", "sharpe")}</div>
  </div>
  <div>
    <h3>最大回撤热力图</h3>
    <div class="card">{make_heatmap_table(ma_pivot_dd, ".2%", "drawdown")}</div>
  </div>
</div>

<h3>Top 10 参数排行（按 Sharpe）</h3>
<div class="card">{make_leaderboard(ma_df, ["short_period", "long_period"])}</div>

{make_stability_summary(ma_stab, "MA Cross 稳定性分析")}

<!-- ═══════════════ RSI ═══════════════ -->
<h2>2️⃣ RSI 反转策略</h2>
<p>参数空间: period × oversold × overbought（{len(RSI_GRID["period"])} × {len(RSI_GRID["oversold"])} × {len(RSI_GRID["overbought"])}）</p>

<h3>Sharpe Ratio 热力图</h3>
<p style="font-size:13px;color:#888">行=RSI周期，列=超卖/超买阈值组合</p>
<div class="card">{make_heatmap_table(rsi_pivot_sharpe, ".3f", "sharpe")}</div>

<h3>收益率热力图</h3>
<div class="card">{make_heatmap_table(rsi_pivot_return, ".2%", "sharpe")}</div>

<h3>Top 10 参数排行</h3>
<div class="card">{make_leaderboard(rsi_df, ["period", "oversold", "overbought"])}</div>

{make_stability_summary(rsi_stab, "RSI 稳定性分析")}

<!-- ═══════════════ Momentum ═══════════════ -->
<h2>3️⃣ Momentum 动量策略</h2>
<p>参数空间: lookback_period × buy_threshold（{len(MOMENTUM_GRID["lookback_period"])} × {len(MOMENTUM_GRID["buy_threshold"])}）</p>

<h3>Sharpe Ratio 热力图</h3>
<p style="font-size:13px;color:#888">行=回看周期，列=买入阈值</p>
<div class="card">{make_heatmap_table(mom_pivot_sharpe, ".3f", "sharpe")}</div>

<h3>收益率热力图</h3>
<div class="card">{make_heatmap_table(mom_pivot_return, ".2%", "sharpe")}</div>

<h3>Top 10 参数排行</h3>
<div class="card">{make_leaderboard(mom_df, ["lookback_period", "buy_threshold"])}</div>

{make_stability_summary(mom_stab, "Momentum 稳定性分析")}

<!-- ═══════════════ 综合结论 ═══════════════ -->
<h2>📋 综合结论</h2>
<div class="verdict">
  <h3>🎯 过拟合风险评估</h3>
  <ul>
    <li><strong>MA Cross</strong>: {"🔴 参数敏感性高，最优点不稳定，存在过拟合风险" if ma_stab.get("overfit_analysis", {}).get("is_likely_overfit") else "🟢 参数区间较稳定，过拟合风险低"}</li>
    <li><strong>RSI</strong>: {"🔴 参数敏感性高，存在过拟合风险" if rsi_stab.get("overfit_analysis", {}).get("is_likely_overfit") else "🟢 参数区间较稳定，过拟合风险低"}</li>
    <li><strong>Momentum</strong>: {"🔴 参数敏感性高，存在过拟合风险" if mom_stab.get("overfit_analysis", {}).get("is_likely_overfit") else "🟢 参数区间较稳定，过拟合风险低"}</li>
  </ul>

  <h3 style="margin-top:20px">💡 优化建议</h3>
  <ul>
    <li>选择<strong>稳定区间的中位数</strong>而非最优点作为参数，降低过拟合风险</li>
    <li>如果热力图中"最优参数"是一个孤立的亮点（周围暗淡），说明该参数组合高度过拟合</li>
    <li>如果热力图呈现大片连续的亮色区域，说明策略在该参数范围内表现稳健</li>
    <li>建议对最终选定的参数做<strong>Walk-Forward 滚动验证</strong>，进一步确认样本外表现</li>
    <li>买入持有 META 一年收益 <code>{buy_hold_return:.2%}</code>，策略必须持续跑赢才有意义</li>
  </ul>
</div>

<p class="footer">AI Quant System — Parameter Sensitivity Analysis | 数据来源: LongPort API</p>
</body>
</html>"""
    return html


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 60)
    print("🔬 参数敏感性分析 — META.US")
    print("=" * 60)

    # 加载数据
    print("\n📂 加载 META 数据...")
    data = load_meta_data()
    print(f"   数据量: {len(data)} 根 K 线, {data['date'].iloc[0]} ~ {data['date'].iloc[-1]}")

    # 买入持有基准
    buy_hold_return = (data["close"].iloc[-1] - data["close"].iloc[0]) / data["close"].iloc[0]
    print(f"   买入持有收益: {buy_hold_return:.2%}")

    # 1. MA Cross 网格搜索
    print("\n" + "=" * 60)
    print("1️⃣ MA Cross 网格搜索")
    print("=" * 60)
    ma_df = grid_search_ma(data)
    ma_stab = stability_analysis(ma_df, ["short_period", "long_period"])

    # 2. RSI 网格搜索
    print("\n" + "=" * 60)
    print("2️⃣ RSI 网格搜索")
    print("=" * 60)
    rsi_df = grid_search_rsi(data)
    rsi_stab = stability_analysis(rsi_df, ["period", "oversold", "overbought"])

    # 3. Momentum 网格搜索
    print("\n" + "=" * 60)
    print("3️⃣ Momentum 网格搜索")
    print("=" * 60)
    mom_df = grid_search_momentum(data)
    mom_stab = stability_analysis(mom_df, ["lookback_period", "buy_threshold"])

    # 保存原始数据
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    ma_df.to_csv(output_dir / "sensitivity_ma_cross.csv", index=False)
    rsi_df.to_csv(output_dir / "sensitivity_rsi.csv", index=False)
    mom_df.to_csv(output_dir / "sensitivity_momentum.csv", index=False)

    # 生成 HTML 报告
    print("\n📊 生成报告...")
    html = generate_html_report(ma_df, rsi_df, mom_df, ma_stab, rsi_stab, mom_stab, buy_hold_return)
    report_path = output_dir / "param_sensitivity_report.html"
    report_path.write_text(html, encoding="utf-8")

    print(f"\n✅ 完成！报告已保存至: {report_path}")
    print(f"   MA Cross: {len(ma_df)} 组 | RSI: {len(rsi_df)} 组 | Momentum: {len(mom_df)} 组")
    print(f"   CSV 数据: {output_dir}/sensitivity_*.csv")


if __name__ == "__main__":
    main()

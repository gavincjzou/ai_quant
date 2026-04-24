"""
单策略参数敏感性分析
====================================
第一步: 仅启用 MA Cross，在 META 上做参数网格搜索
第二步: 仅启用 RSI，在 META 上做参数网格搜索
固定风控和执行参数不变
输出: total_return, annual_return, max_drawdown, sharpe_ratio, win_rate, trade_count
"""

import sys, os, warnings, itertools
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ["LOGURU_LEVEL"] = "ERROR"  # 抑制 loguru 输出，只要结果

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.ma_cross_strategy import MACrossStrategy
from src.strategy.rsi_strategy import RSIStrategy
from src.backtest.engine import BacktestEngine

# ================================================================
# 固定参数（风控 + 执行，两步完全一致）
# ================================================================
INITIAL_CAPITAL = 102564.0  # HK$800k ≈ US$102,564
COMMISSION = {"type": "per_share", "rate": 0.0049, "platform_fee": 0.005}
SLIPPAGE = {"type": "percentage", "value": 0.001}

# ================================================================
# MA Cross 参数网格（更细致）
# ================================================================
MA_SHORT_PERIODS = [3, 5, 8, 10, 13, 15, 20, 25]      # 8 个
MA_LONG_PERIODS  = [20, 30, 40, 50, 60, 80, 100, 120, 150, 200]  # 10 个
MA_SIGNAL_TYPES  = ["SMA", "EMA"]  # 2 种

# ================================================================
# RSI 参数网格（更细致）
# ================================================================
RSI_PERIODS    = [7, 10, 14, 20, 28]        # 5 个
RSI_OVERSOLD   = [15, 20, 25, 30, 35, 40]   # 6 个
RSI_OVERBOUGHT = [60, 65, 70, 75, 80, 85]   # 6 个


def load_meta_data() -> pd.DataFrame:
    csv_path = PROJECT_ROOT / "data_cache" / "csv" / "META_US_1d.csv"
    df = pd.read_csv(csv_path, parse_dates=["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def run_backtest(strategy_cls, config: dict, data: pd.DataFrame) -> dict:
    """运行一次回测，返回 6 个核心指标"""
    try:
        strat = strategy_cls()
        strat.init(config)
        engine = BacktestEngine({"backtest": {
            "initial_capital": INITIAL_CAPITAL,
            "commission": COMMISSION,
            "slippage": SLIPPAGE,
        }})
        result = engine.run(strat, data, symbol="META.US", initial_capital=INITIAL_CAPITAL)
        m = result["metrics"]
        return {
            "total_return":  m.get("total_return", 0),
            "annual_return": m.get("annual_return", 0),
            "max_drawdown":  m.get("max_drawdown", 0),
            "sharpe_ratio":  m.get("sharpe_ratio", 0) or 0,
            "win_rate":      m.get("win_rate", 0),
            "trade_count":   int(m.get("trade_count", 0)),
        }
    except Exception as e:
        return {
            "total_return": None, "annual_return": None,
            "max_drawdown": None, "sharpe_ratio": None,
            "win_rate": None, "trade_count": None,
            "error": str(e),
        }


# ================================================================
# 第一步: MA Cross 网格搜索
# ================================================================
def step1_ma_cross(data: pd.DataFrame) -> pd.DataFrame:
    print("=" * 70)
    print("📊 第一步: MA Cross 单策略参数敏感性分析")
    print("=" * 70)
    print(f"   参数空间: short_period({len(MA_SHORT_PERIODS)}) × long_period({len(MA_LONG_PERIODS)}) × signal_type({len(MA_SIGNAL_TYPES)})")

    rows = []
    combos = [(sp, lp, st) for sp in MA_SHORT_PERIODS
              for lp in MA_LONG_PERIODS
              for st in MA_SIGNAL_TYPES
              if sp < lp]  # 短周期必须 < 长周期

    total = len(combos)
    print(f"   有效组合数: {total}")
    print()

    for i, (sp, lp, st) in enumerate(combos, 1):
        cfg = {"short_period": sp, "long_period": lp, "signal_type": st}
        m = run_backtest(MACrossStrategy, cfg, data)
        m["short_period"] = sp
        m["long_period"] = lp
        m["signal_type"] = st
        rows.append(m)

        ret = m["total_return"]
        sharpe = m["sharpe_ratio"]
        trades = m["trade_count"]
        status = f"return={ret:+.2%}, sharpe={sharpe:+.3f}, trades={trades}" if ret is not None else "FAIL"
        print(f"  [{i:3d}/{total}] {st} MA({sp},{lp}) → {status}")

    df = pd.DataFrame(rows)
    return df


# ================================================================
# 第二步: RSI 网格搜索
# ================================================================
def step2_rsi(data: pd.DataFrame) -> pd.DataFrame:
    print()
    print("=" * 70)
    print("📊 第二步: RSI 单策略参数敏感性分析")
    print("=" * 70)
    print(f"   参数空间: period({len(RSI_PERIODS)}) × oversold({len(RSI_OVERSOLD)}) × overbought({len(RSI_OVERBOUGHT)})")

    rows = []
    combos = [(p, os, ob) for p in RSI_PERIODS
              for os in RSI_OVERSOLD
              for ob in RSI_OVERBOUGHT
              if os < ob]  # 超卖必须 < 超买

    total = len(combos)
    print(f"   有效组合数: {total}")
    print()

    for i, (period, os_val, ob_val) in enumerate(combos, 1):
        cfg = {"period": period, "oversold": os_val, "overbought": ob_val}
        m = run_backtest(RSIStrategy, cfg, data)
        m["period"] = period
        m["oversold"] = os_val
        m["overbought"] = ob_val
        rows.append(m)

        ret = m["total_return"]
        sharpe = m["sharpe_ratio"]
        trades = m["trade_count"]
        status = f"return={ret:+.2%}, sharpe={sharpe:+.3f}, trades={trades}" if ret is not None else "FAIL"
        print(f"  [{i:3d}/{total}] RSI({period}, {os_val}/{ob_val}) → {status}")

    df = pd.DataFrame(rows)
    return df


# ================================================================
# HTML 报告生成
# ================================================================
def generate_report(ma_df: pd.DataFrame, rsi_df: pd.DataFrame, buy_hold: float) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def heatmap_html(pivot: pd.DataFrame, fmt_func, label: str, invert: bool = False) -> str:
        """生成热力图 HTML"""
        if pivot.empty:
            return "<p>无数据</p>"
        vals = pivot.values.flatten()
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            return "<p>无有效数据</p>"
        vmin, vmax = float(np.nanmin(vals)), float(np.nanmax(vals))

        html = f'<table class="hm"><thead><tr><th>{label}</th>'
        for c in pivot.columns:
            html += f"<th>{c}</th>"
        html += "</tr></thead><tbody>"

        for idx in pivot.index:
            html += f"<tr><td class='rh'>{idx}</td>"
            for c in pivot.columns:
                v = pivot.loc[idx, c]
                if pd.isna(v):
                    html += "<td class='na'>—</td>"
                else:
                    norm = (v - vmin) / (vmax - vmin) if vmax > vmin else 0.5
                    if invert:
                        norm = 1 - norm
                    # 颜色：红(差) → 黄(中) → 绿(好)
                    if norm < 0.5:
                        r, g = 220, int(100 + 200 * norm)
                        b = 80
                    else:
                        r = int(220 - 180 * (norm - 0.5) * 2)
                        g, b = 200, 80
                    tc = "#000" if norm > 0.3 else "#fff"
                    html += f'<td style="background:rgb({r},{g},{b});color:{tc}">{fmt_func(v)}</td>'
            html += "</tr>"
        html += "</tbody></table>"
        return html

    def fmt_pct(v): return f"{v:+.1%}"
    def fmt_f3(v): return f"{v:+.3f}"
    def fmt_f1(v): return f"{v:.1%}"
    def fmt_d(v): return f"{int(v)}"

    def make_full_table(df: pd.DataFrame, param_cols: list) -> str:
        """完整数据表（可排序）"""
        valid = df.dropna(subset=["sharpe_ratio"]).sort_values("sharpe_ratio", ascending=False)
        html = '<table class="dt"><thead><tr>'
        html += '<th>#</th>'
        for c in param_cols:
            html += f'<th>{c}</th>'
        html += '<th>总收益</th><th>年化</th><th>最大回撤</th><th>Sharpe</th><th>胜率</th><th>交易数</th></tr></thead><tbody>'
        for i, (_, row) in enumerate(valid.iterrows(), 1):
            # 行色：前 10 绿底，后 10 红底
            cls = ""
            if i <= 5:
                cls = ' class="top"'
            elif i > len(valid) - 5:
                cls = ' class="bot"'
            html += f'<tr{cls}><td>{i}</td>'
            for c in param_cols:
                html += f'<td>{row[c]}</td>'
            tr = row["total_return"]
            ar = row["annual_return"]
            md = row["max_drawdown"]
            sr = row["sharpe_ratio"]
            wr = row["win_rate"]
            tc = row["trade_count"]
            html += f'<td>{tr:+.2%}</td><td>{ar:+.2%}</td><td>{md:.2%}</td>'
            html += f'<td>{sr:+.3f}</td><td>{wr:.1%}</td><td>{int(tc)}</td></tr>'
        html += '</tbody></table>'
        return html

    def stability_stats(df: pd.DataFrame, metric: str = "sharpe_ratio") -> dict:
        valid = df.dropna(subset=[metric])
        if valid.empty:
            return {}
        vals = valid[metric]
        return {
            "count": len(vals),
            "mean": float(vals.mean()),
            "median": float(vals.median()),
            "std": float(vals.std()),
            "min": float(vals.min()),
            "max": float(vals.max()),
            "q25": float(vals.quantile(0.25)),
            "q75": float(vals.quantile(0.75)),
            "pct_positive": float((vals > 0).mean()),
            "pct_beat_bh": float((valid["total_return"] > buy_hold).mean()),
        }

    ma_stats = stability_stats(ma_df)
    rsi_stats = stability_stats(rsi_df)

    # MA Cross 热力图（SMA 和 EMA 分开）
    ma_valid = ma_df.dropna(subset=["sharpe_ratio"])
    ma_sma = ma_valid[ma_valid["signal_type"] == "SMA"]
    ma_ema = ma_valid[ma_valid["signal_type"] == "EMA"]

    sma_pivot_sr = ma_sma.pivot_table(index="short_period", columns="long_period", values="sharpe_ratio")
    sma_pivot_tr = ma_sma.pivot_table(index="short_period", columns="long_period", values="total_return")
    sma_pivot_dd = ma_sma.pivot_table(index="short_period", columns="long_period", values="max_drawdown")
    sma_pivot_tc = ma_sma.pivot_table(index="short_period", columns="long_period", values="trade_count")

    ema_pivot_sr = ma_ema.pivot_table(index="short_period", columns="long_period", values="sharpe_ratio")
    ema_pivot_tr = ma_ema.pivot_table(index="short_period", columns="long_period", values="total_return")

    # RSI 热力图（固定 period=14 做 oversold × overbought 切面）
    rsi_valid = rsi_df.dropna(subset=["sharpe_ratio"])
    rsi_14 = rsi_valid[rsi_valid["period"] == 14]
    rsi_14_sr = rsi_14.pivot_table(index="oversold", columns="overbought", values="sharpe_ratio")
    rsi_14_tr = rsi_14.pivot_table(index="oversold", columns="overbought", values="total_return")
    rsi_14_dd = rsi_14.pivot_table(index="oversold", columns="overbought", values="max_drawdown")

    # RSI period 维度（取各 period 下 Sharpe 中位数 / 均值）
    rsi_by_period = rsi_valid.groupby("period").agg(
        sharpe_mean=("sharpe_ratio", "mean"),
        sharpe_median=("sharpe_ratio", "median"),
        return_mean=("total_return", "mean"),
        return_median=("total_return", "median"),
        count=("sharpe_ratio", "count"),
    ).reset_index()

    # 判断参数敏感性（"参数稍微变动，收益大幅下降"）
    def sensitivity_verdict(stats: dict, name: str) -> str:
        if not stats:
            return f"{name}: 无有效数据"
        iqr = stats["q75"] - stats["q25"]
        spread = stats["max"] - stats["min"]
        cv = stats["std"] / abs(stats["mean"]) if abs(stats["mean"]) > 0.001 else 999

        lines = []
        lines.append(f"<strong>{name}</strong>")
        lines.append(f"Sharpe 范围: [{stats['min']:+.3f}, {stats['max']:+.3f}]，中位数 {stats['median']:+.3f}")
        lines.append(f"标准差: {stats['std']:.3f}，IQR: {iqr:.3f}，变异系数(CV): {cv:.2f}")
        lines.append(f"Sharpe > 0 的比例: {stats['pct_positive']:.1%}")
        lines.append(f"跑赢买入持有的比例: {stats['pct_beat_bh']:.1%}")

        if cv > 3:
            lines.append('⚠️ <span class="red">参数极度敏感（CV > 3），高过拟合风险</span>')
        elif cv > 1.5:
            lines.append('⚠️ <span class="yellow">参数较敏感（CV > 1.5），中等过拟合风险</span>')
        else:
            lines.append('✅ <span class="green">参数较稳定（CV < 1.5），过拟合风险低</span>')

        if stats["pct_beat_bh"] < 0.1:
            lines.append('🔴 <span class="red">超过 90% 的参数组合跑不赢买入持有，策略本身可能无效</span>')
        elif stats["pct_beat_bh"] < 0.3:
            lines.append('🟡 <span class="yellow">仅少数参数组合跑赢买入持有，需谨慎选参</span>')
        else:
            lines.append('🟢 <span class="green">较多参数组合跑赢买入持有，策略有基础alpha</span>')

        return "<br>".join(lines)

    ma_verdict = sensitivity_verdict(ma_stats, "MA Cross")
    rsi_verdict = sensitivity_verdict(rsi_stats, "RSI")

    # RSI period 维度 HTML 表
    rsi_period_rows = []
    for _, r in rsi_by_period.iterrows():
        p = int(r["period"])
        c = int(r["count"])
        sm = r["sharpe_mean"]
        smed = r["sharpe_median"]
        rm = r["return_mean"]
        rmed = r["return_median"]
        rsi_period_rows.append(
            "<tr><td>%d</td><td>%d</td><td>%+.3f</td><td>%+.3f</td><td>%+.2f%%</td><td>%+.2f%%</td></tr>"
            % (p, c, sm, smed, rm * 100, rmed * 100)
        )
    rsi_period_html = (
        '<table class="dt"><thead><tr><th>RSI Period</th><th>组合数</th>'
        '<th>Sharpe 均值</th><th>Sharpe 中位数</th><th>收益均值</th><th>收益中位数</th>'
        '</tr></thead><tbody>' + "".join(rsi_period_rows) + '</tbody></table>'
    )

    # 预计算所有模板变量（避免 f-string 中的复杂表达式）
    bh_color = "green" if buy_hold >= 0 else "red"
    bh_str = "%+.2f%%" % (buy_hold * 100)
    ma_count = len(ma_df)
    rsi_count = len(rsi_df)
    ma_med = ma_stats.get("median", 0)
    rsi_med = rsi_stats.get("median", 0)
    ma_med_color = "green" if ma_med > 0 else "red"
    rsi_med_color = "green" if rsi_med > 0 else "red"
    ma_med_str = "%+.3f" % ma_med
    rsi_med_str = "%+.3f" % rsi_med

    ma_std_str = "%.3f" % ma_stats.get("std", 0)
    rsi_std_str = "%.3f" % rsi_stats.get("std", 0)
    ma_pos_str = "%.1f%%" % (ma_stats.get("pct_positive", 0) * 100)
    rsi_pos_str = "%.1f%%" % (rsi_stats.get("pct_positive", 0) * 100)
    ma_bh_str = "%.1f%%" % (ma_stats.get("pct_beat_bh", 0) * 100)
    rsi_bh_str = "%.1f%%" % (rsi_stats.get("pct_beat_bh", 0) * 100)
    ma_best_str = "%+.3f" % ma_stats.get("max", 0)
    rsi_best_str = "%+.3f" % rsi_stats.get("max", 0)
    ma_worst_str = "%+.3f" % ma_stats.get("min", 0)
    rsi_worst_str = "%+.3f" % rsi_stats.get("min", 0)

    # 生成热力图片段
    sma_sr_html = heatmap_html(sma_pivot_sr, fmt_f3, "Short \\\\ Long")
    sma_tr_html = heatmap_html(sma_pivot_tr, fmt_pct, "Short \\\\ Long")
    sma_dd_html = heatmap_html(sma_pivot_dd, fmt_pct, "Short \\\\ Long", invert=True)
    ema_sr_html = heatmap_html(ema_pivot_sr, fmt_f3, "Short \\\\ Long")
    ema_tr_html = heatmap_html(ema_pivot_tr, fmt_pct, "Short \\\\ Long")
    rsi14_sr_html = heatmap_html(rsi_14_sr, fmt_f3, "Oversold \\\\ Overbought")
    rsi14_tr_html = heatmap_html(rsi_14_tr, fmt_pct, "Oversold \\\\ Overbought")
    rsi14_dd_html = heatmap_html(rsi_14_dd, fmt_pct, "Oversold \\\\ Overbought", invert=True)
    ma_table_html = make_full_table(ma_df, ["short_period", "long_period", "signal_type"])
    rsi_table_html = make_full_table(rsi_df, ["period", "oversold", "overbought"])

    # 用 % 格式化构建 HTML（兼容 Python 3.8）
    tpl = {
        "now": now,
        "bh_color": bh_color, "bh_str": bh_str,
        "ma_count": ma_count, "rsi_count": rsi_count,
        "ma_med_color": ma_med_color, "ma_med_str": ma_med_str,
        "rsi_med_color": rsi_med_color, "rsi_med_str": rsi_med_str,
        "sma_sr": sma_sr_html, "sma_tr": sma_tr_html, "sma_dd": sma_dd_html,
        "ema_sr": ema_sr_html, "ema_tr": ema_tr_html,
        "ma_table": ma_table_html, "ma_verdict": ma_verdict,
        "rsi_period_tbl": rsi_period_html,
        "rsi14_sr": rsi14_sr_html, "rsi14_tr": rsi14_tr_html, "rsi14_dd": rsi14_dd_html,
        "rsi_table": rsi_table_html, "rsi_verdict": rsi_verdict,
        "ma_std": ma_std_str, "rsi_std": rsi_std_str,
        "ma_pos": ma_pos_str, "rsi_pos": rsi_pos_str,
        "ma_bh": ma_bh_str, "rsi_bh": rsi_bh_str,
        "ma_best": ma_best_str, "rsi_best": rsi_best_str,
        "ma_worst": ma_worst_str, "rsi_worst": rsi_worst_str,
    }

    html = (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<title>单策略参数敏感性分析 — META.US</title>\n<style>\n'
        '* { margin:0; padding:0; box-sizing:border-box; }\n'
        "body { font-family:-apple-system,'Segoe UI',sans-serif; background:#0a0a0a; color:#ddd; padding:24px; line-height:1.6; }\n"
        'h1 { text-align:center; font-size:28px; color:#fff; margin-bottom:6px; }\n'
        '.sub { text-align:center; color:#888; font-size:14px; margin-bottom:28px; }\n'
        'h2 { font-size:22px; color:#60a5fa; border-bottom:2px solid #1e3a5f; padding-bottom:8px; margin:36px 0 16px; }\n'
        'h3 { font-size:17px; color:#93c5fd; margin:20px 0 10px; }\n'
        '.bar { display:flex; justify-content:center; gap:24px; flex-wrap:wrap; margin:20px 0 32px; }\n'
        '.bar .item { background:#151525; border:1px solid #2a2a4a; border-radius:8px; padding:12px 20px; text-align:center; min-width:120px; }\n'
        '.bar .item .l { font-size:11px; color:#888; }\n'
        '.bar .item .v { font-size:20px; font-weight:700; margin-top:4px; }\n'
        '.green { color:#22c55e; } .red { color:#ef4444; } .yellow { color:#fbbf24; } .blue { color:#60a5fa; }\n'
        '.card { background:#141422; border:1px solid #2a2a4a; border-radius:10px; padding:16px; overflow-x:auto; margin:12px 0; }\n'
        '.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }\n'
        '@media(max-width:960px){ .grid2 { grid-template-columns:1fr; } }\n'
        'table.hm { border-collapse:collapse; width:100%%; font-size:13px; }\n'
        'table.hm th,table.hm td { padding:7px 10px; text-align:center; border:1px solid #333; }\n'
        'table.hm th { background:#1e1e3e; color:#93c5fd; font-size:12px; }\n'
        'td.rh { background:#1e1e3e; color:#93c5fd; font-weight:700; }\n'
        'td.na { color:#555; }\n'
        'table.dt { border-collapse:collapse; width:100%%; font-size:12px; }\n'
        'table.dt th { background:#1e1e3e; color:#60a5fa; padding:7px 8px; border:1px solid #333; position:sticky; top:0; }\n'
        'table.dt td { padding:6px 8px; border:1px solid #2a2a3a; text-align:center; }\n'
        'table.dt tr:nth-child(even) { background:#151525; }\n'
        'table.dt tr.top td { background:#1a3a1a; color:#22c55e; font-weight:600; }\n'
        'table.dt tr.bot td { background:#3a1a1a; color:#ef4444; }\n'
        '.verdict { background:#151525; border:2px solid #3b82f6; border-radius:12px; padding:24px; margin:24px 0; line-height:2; }\n'
        '.verdict h3 { color:#fbbf24; margin-bottom:12px; }\n'
        '.scroll { max-height:500px; overflow-y:auto; }\n'
        '.foot { text-align:center; color:#555; font-size:12px; margin-top:40px; }\n'
        '</style>\n</head>\n<body>\n\n'
        '<h1>🔬 单策略参数敏感性分析</h1>\n'
        '<p class="sub">META.US · 2025-04 ~ 2026-04 · 初始资金 $102,564 · %(now)s</p>\n\n'
        '<div class="bar">\n'
        '  <div class="item"><div class="l">买入持有收益</div><div class="v %(bh_color)s">%(bh_str)s</div></div>\n'
        '  <div class="item"><div class="l">MA Cross 组合</div><div class="v blue">%(ma_count)s</div></div>\n'
        '  <div class="item"><div class="l">RSI 组合</div><div class="v blue">%(rsi_count)s</div></div>\n'
        '  <div class="item"><div class="l">MA Sharpe 中位数</div><div class="v %(ma_med_color)s">%(ma_med_str)s</div></div>\n'
        '  <div class="item"><div class="l">RSI Sharpe 中位数</div><div class="v %(rsi_med_color)s">%(rsi_med_str)s</div></div>\n'
        '</div>\n\n'
        '<h2>📈 第一步: MA Cross 均线交叉策略</h2>\n'
        '<p>参数: short_period × long_period × signal_type | 固定: 佣金 $0.0049/股, 滑点 0.1%%, 初始资金 $102,564</p>\n\n'
        '<h3>SMA — Sharpe Ratio 热力图 <span style="font-size:12px;color:#888">(行=短周期, 列=长周期)</span></h3>\n'
        '<div class="card">%(sma_sr)s</div>\n\n'
        '<div class="grid2">\n<div>\n<h3>SMA — 总收益率热力图</h3>\n<div class="card">%(sma_tr)s</div>\n</div>\n'
        '<div>\n<h3>SMA — 最大回撤热力图</h3>\n<div class="card">%(sma_dd)s</div>\n</div>\n</div>\n\n'
        '<h3>EMA — Sharpe Ratio 热力图</h3>\n<div class="card">%(ema_sr)s</div>\n\n'
        '<h3>EMA — 总收益率热力图</h3>\n<div class="card">%(ema_tr)s</div>\n\n'
        '<h3>MA Cross 完整数据表（按 Sharpe 降序）</h3>\n<div class="card scroll">%(ma_table)s</div>\n\n'
        '<div class="verdict">\n<h3>🔍 MA Cross 稳定性诊断</h3>\n<p>%(ma_verdict)s</p>\n</div>\n\n'
        '<h2>📉 第二步: RSI 反转策略</h2>\n'
        '<p>参数: period × oversold × overbought | 固定: 佣金 $0.0049/股, 滑点 0.1%%, 初始资金 $102,564</p>\n\n'
        '<h3>RSI Period 维度聚合统计</h3>\n<div class="card">%(rsi_period_tbl)s</div>\n\n'
        '<h3>RSI(14) — Sharpe Ratio 热力图 <span style="font-size:12px;color:#888">(行=超卖, 列=超买)</span></h3>\n'
        '<div class="card">%(rsi14_sr)s</div>\n\n'
        '<div class="grid2">\n<div>\n<h3>RSI(14) — 总收益率热力图</h3>\n<div class="card">%(rsi14_tr)s</div>\n</div>\n'
        '<div>\n<h3>RSI(14) — 最大回撤热力图</h3>\n<div class="card">%(rsi14_dd)s</div>\n</div>\n</div>\n\n'
        '<h3>RSI 完整数据表（按 Sharpe 降序）</h3>\n<div class="card scroll">%(rsi_table)s</div>\n\n'
        '<div class="verdict">\n<h3>🔍 RSI 稳定性诊断</h3>\n<p>%(rsi_verdict)s</p>\n</div>\n\n'
        '<h2>📋 综合结论</h2>\n'
        '<div class="verdict">\n'
        '<h3>🎯 单策略稳定性对比</h3>\n'
        '<table class="dt" style="max-width:700px;margin:12px auto;">\n'
        '<thead><tr><th></th><th>MA Cross</th><th>RSI</th></tr></thead>\n<tbody>\n'
        '<tr><td>参数组合数</td><td>%(ma_count)s</td><td>%(rsi_count)s</td></tr>\n'
        '<tr><td>Sharpe 中位数</td><td>%(ma_med_str)s</td><td>%(rsi_med_str)s</td></tr>\n'
        '<tr><td>Sharpe 标准差</td><td>%(ma_std)s</td><td>%(rsi_std)s</td></tr>\n'
        '<tr><td>Sharpe > 0 比例</td><td>%(ma_pos)s</td><td>%(rsi_pos)s</td></tr>\n'
        '<tr><td>跑赢买入持有比例</td><td>%(ma_bh)s</td><td>%(rsi_bh)s</td></tr>\n'
        '<tr><td>最佳 Sharpe</td><td>%(ma_best)s</td><td>%(rsi_best)s</td></tr>\n'
        '<tr><td>最差 Sharpe</td><td>%(ma_worst)s</td><td>%(rsi_worst)s</td></tr>\n'
        '</tbody></table>\n\n'
        '<h3 style="margin-top:20px;">💡 结论与下一步</h3>\n'
        '<ul style="margin:12px 0 0 24px; line-height:2;">\n'
        '<li>如果某策略 <strong>中位数 Sharpe &lt; 0</strong> 且 <strong>跑赢买入持有比例 &lt; 10%%</strong>，说明该策略在 META 上<strong>不具备 alpha</strong></li>\n'
        '<li>如果某策略 <strong>变异系数 CV &gt; 2</strong>，说明<strong>参数稍微变动收益大幅下降</strong>，过拟合风险高</li>\n'
        '<li>稳定的策略应该有：大片连续的正 Sharpe 区域（热力图中大面积亮色），而不是孤立亮点</li>\n'
        '<li>建议对通过筛选的策略做 <strong>Walk-Forward 滚动验证</strong>，再考虑实盘</li>\n'
        '</ul>\n</div>\n\n'
        '<p class="foot">AI Quant System — Single Strategy Sensitivity · META.US · %(now)s</p>\n'
        '</body></html>'
    ) % tpl
    return html


# ================================================================
# 主流程
# ================================================================
def main():
    print("🔬 单策略参数敏感性分析 — META.US")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()

    data = load_meta_data()
    print(f"📂 数据: {len(data)} 根 K 线, {data['date'].iloc[0].date()} ~ {data['date'].iloc[-1].date()}")
    buy_hold = (data["close"].iloc[-1] - data["close"].iloc[0]) / data["close"].iloc[0]
    print(f"📊 买入持有收益: {buy_hold:+.2%}")
    print()

    # 第一步: MA Cross
    ma_df = step1_ma_cross(data)

    # 第二步: RSI
    rsi_df = step2_rsi(data)

    # 保存 CSV
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    ma_df.to_csv(output_dir / "sensitivity_ma_cross_single.csv", index=False)
    rsi_df.to_csv(output_dir / "sensitivity_rsi_single.csv", index=False)
    print(f"\n📁 CSV 已保存: output/sensitivity_ma_cross_single.csv, output/sensitivity_rsi_single.csv")

    # 生成 HTML 报告
    html = generate_report(ma_df, rsi_df, buy_hold)
    report_path = output_dir / "single_strategy_sensitivity.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"📊 HTML 报告: {report_path}")

    # 打印摘要
    print()
    print("=" * 70)
    print("📋 摘要")
    print("=" * 70)
    for name, df in [("MA Cross", ma_df), ("RSI", rsi_df)]:
        valid = df.dropna(subset=["sharpe_ratio"])
        sr = valid["sharpe_ratio"]
        tr = valid["total_return"]
        print(f"\n  {name}: {len(valid)} 组有效")
        print(f"    Sharpe: [{sr.min():+.3f}, {sr.max():+.3f}], 中位数 {sr.median():+.3f}, 均值 {sr.mean():+.3f}")
        print(f"    收益:   [{tr.min():+.2%}, {tr.max():+.2%}], 中位数 {tr.median():+.2%}")
        print(f"    Sharpe>0 占比: {(sr>0).mean():.1%}")
        print(f"    跑赢买入持有: {(tr>buy_hold).mean():.1%}")


if __name__ == "__main__":
    main()

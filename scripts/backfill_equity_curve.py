#!/usr/bin/env python3
"""
阶段8 Fix Round 3：补回历史每日净值

用现有 trade_records + K 线收盘价，重建 daily_performance 表的历史数据。

逻辑：
1. 从 trade_records 最早日期开始，按日逐步推进
2. 每天：
   - 按顺序重放当天的 trades（买/卖 → 更新 positions + cash）
   - 用当天收盘价重新估值所有持仓
   - 写一条 daily_performance 记录
3. 用 INSERT OR REPLACE 保证幂等

使用方法：
    python scripts/backfill_equity_curve.py               # 补回全部历史
    python scripts/backfill_equity_curve.py --from 2026-04-22   # 从指定日期开始

注意：
- 本脚本只修复 daily_performance 表，不改 trade_records 或 positions
- 初始资金从 settings.yaml 读
"""
import argparse
import os
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from src.data.database import DatabaseManager
from src.utils.config_loader import ConfigLoader


def get_close_price_for(db: DatabaseManager, symbol: str, target_date: date) -> float:
    """
    找 <= target_date 的最近一天收盘价。
    如果 target_date 当天是交易日直接取；否则回溯到最近的交易日。
    """
    try:
        df = db.load_kline(symbol, period="1d")
        if df is None or df.empty:
            return 0.0
        # 日期列可能是 datetime 或 str，统一成 date
        if "date" in df.columns:
            df["_d"] = pd.to_datetime(df["date"]).dt.date
        else:
            df["_d"] = pd.to_datetime(df.index).date
        df = df[df["_d"] <= target_date].sort_values("_d")
        if df.empty:
            return 0.0
        return float(df.iloc[-1]["close"])
    except Exception as e:
        logger.warning(f"get_close_price_for({symbol}, {target_date}) 失败：{e}")
        return 0.0


def backfill(project_root: str, from_date: date = None, initial_capital: float = 800000.0):
    db_path = os.path.join(project_root, "data_cache", "quant.db")
    db = DatabaseManager(db_path)

    # 1. 读所有 trade_records
    trades_df = db.load_trades(trade_mode="paper")
    if trades_df is None or trades_df.empty:
        logger.warning("无 trade_records 记录，退出")
        return

    trades_df["_date"] = pd.to_datetime(trades_df["executed_at"]).dt.date
    trades_df = trades_df.sort_values("executed_at")

    earliest = trades_df["_date"].min()
    today = date.today()
    start = from_date or earliest

    logger.info(f"backfill 范围：{start} → {today}")
    logger.info(f"trade_records 最早日期：{earliest}")

    # 2. 逐日推进
    # 先跳过 start 之前的，但要先把之前的 trades 应用到初始持仓
    cash = initial_capital
    positions: Dict[str, dict] = {}

    # 应用所有 <= (start-1) 的 trades 得到起始状态
    warmup = trades_df[trades_df["_date"] < start]
    for _, t in warmup.iterrows():
        apply_trade(positions, t, cash_ref=[cash])
        cash = _current_cash(positions, cash, t)

    # 逐日
    cur = start
    stats = {"written": 0, "skipped_no_data": 0}
    while cur <= today:
        # 当天 trades
        day_trades = trades_df[trades_df["_date"] == cur]
        cash_list = [cash]
        for _, t in day_trades.iterrows():
            apply_trade(positions, t, cash_ref=cash_list)
        cash = cash_list[0]

        # 当天收盘价估值
        market_value = 0.0
        missing = []
        for sym, p in list(positions.items()):
            if p["quantity"] <= 0:
                del positions[sym]
                continue
            px = get_close_price_for(db, sym, cur)
            if px <= 0:
                missing.append(sym)
                # 用成本价兜底
                px = p["avg_cost"]
            p["current_price"] = px
            p["market_value"] = px * p["quantity"]
            market_value += p["market_value"]

        total_assets = cash + market_value
        cum_return = (total_assets - initial_capital) / initial_capital if initial_capital > 0 else 0.0

        # 只在交易日 / 或有交易的日子写
        # 简化：每天都写（反正 INSERT OR REPLACE 幂等）
        snapshot = {
            "trade_mode": "paper",
            "date": cur.isoformat(),
            "total_assets": round(total_assets, 2),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "daily_pnl": 0.0,
            "daily_return": 0.0,
            "cumulative_return": round(cum_return, 6),
            "max_drawdown": 0.0,
            "trade_count": len(day_trades),
        }
        db.save_daily_performance(snapshot)
        stats["written"] += 1

        # 如果是周末就跳过（数据源不会有新收盘价）
        if cur.weekday() >= 5:  # 5=Sat, 6=Sun
            stats["skipped_no_data"] += 1

        mark = f"📊 {cur}" + (f" ⚠️缺价：{missing}" if missing else "")
        logger.info(f"{mark} | total=${total_assets:,.2f} cash=${cash:,.0f} mv=${market_value:,.0f} "
                    f"ret={cum_return:+.2%} trades={len(day_trades)}")

        cur += timedelta(days=1)

    logger.info(f"✅ 补回完成：写入 {stats['written']} 天（含 {stats['skipped_no_data']} 个周末）")


def apply_trade(positions: Dict[str, dict], trade_row, cash_ref: List[float]):
    """把一笔 trade 应用到 positions/cash。cash_ref 是 [cash] 的引用。"""
    sym = trade_row["symbol"]
    qty = int(trade_row["quantity"])
    price = float(trade_row["price"])
    comm = float(trade_row.get("commission", 0) or 0)

    if trade_row["side"] == "buy":
        if sym in positions:
            old = positions[sym]
            new_qty = old["quantity"] + qty
            new_avg = (old["avg_cost"] * old["quantity"] + price * qty) / new_qty
            positions[sym] = {
                "quantity": new_qty,
                "avg_cost": new_avg,
                "current_price": price,
                "market_value": new_qty * price,
            }
        else:
            positions[sym] = {
                "quantity": qty,
                "avg_cost": price,
                "current_price": price,
                "market_value": qty * price,
            }
        cash_ref[0] -= (qty * price + comm)
    else:  # sell
        if sym in positions:
            positions[sym]["quantity"] -= qty
            if positions[sym]["quantity"] <= 0:
                del positions[sym]
        cash_ref[0] += (qty * price - comm)


def _current_cash(positions, cash, trade_row):
    """已废弃：apply_trade 已通过 cash_ref 更新，这里兼容旧调用"""
    return cash  # apply_trade 里已经更新了 ref


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", type=str, default=None,
                        help="开始日期 YYYY-MM-DD，默认从最早 trade 开始")
    parser.add_argument("--capital", type=float, default=800000.0,
                        help="初始资金，默认 800000")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    from_date = None
    if args.from_date:
        from_date = date.fromisoformat(args.from_date)

    backfill(project_root, from_date=from_date, initial_capital=args.capital)


if __name__ == "__main__":
    main()

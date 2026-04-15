#!/usr/bin/env python3
"""
MTT 4h Trend Following PoC — Turtle風 Donchian Breakout

仮説: 15m で50+通り失敗した原因は noise。4h なら:
  - noise 1/16
  - 手数料負担 1/16
  - 取引頻度 1/16 だが signal quality 向上

設計 (Turtle System 風):
  - 4h バー
  - Entry: 20バー高値ブレイク → LONG / 20バー安値ブレイク → SHORT
  - Exit: 10バー反対側 or 2 ATR SL
  - TP: 3 ATR
  - Position size: ATR based (1% risk / ATR)
  - 対象: BTC, ETH, XRP, SOL, BNB (Bybit 4h 12ヶ月)

BUG 修正済み: サイジングは risk_amt / stop_distance (正しい式)
"""

import argparse
import json
import sys
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from collections import Counter

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest_live_design import fetch_ccxt_long, calc_atr


SYMBOLS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "BNB/USDT"]


@dataclass
class Config4H:
    entry_lookback: int = 20          # 20バー高値/安値 (Turtle)
    exit_lookback: int = 10           # 10バー反対側 (Turtle exit)
    atr_period: int = 14
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    use_tp: bool = True               # TP使うかTurtleのみ使うか
    min_atr_pct: float = 0.002        # 4hでより高いボラ閾値
    cooldown_bars: int = 2            # 8時間
    risk_per_trade: float = 0.01
    initial_capital: float = 100000.0
    commission_rate: float = 0.001
    slippage_rate: float = 0.0005


def backtest_4h(df: pd.DataFrame, config: Config4H, symbol: str) -> dict:
    atr = calc_atr(df, period=config.atr_period)
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    trades = []
    capital = config.initial_capital
    position = 0
    entry_price = 0.0
    entry_atr = 0.0
    entry_idx = -1
    position_size = 0.0
    cooldown_until_idx = -1

    lookback_entry = config.entry_lookback
    lookback_exit = config.exit_lookback

    for i in range(max(lookback_entry, 30), len(df)):
        current_atr = atr.iloc[i]
        if pd.isna(current_atr) or current_atr <= 0:
            continue

        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]

        # 保有中の判定
        if position != 0:
            sl_dist = entry_atr * config.sl_atr_mult
            tp_dist = entry_atr * config.tp_atr_mult
            exit_price = None
            reason = None

            if position == 1:
                # SL
                if current_low <= entry_price - sl_dist:
                    exit_price = entry_price - sl_dist
                    reason = "STOP_LOSS"
                # TP
                elif config.use_tp and current_high >= entry_price + tp_dist:
                    exit_price = entry_price + tp_dist
                    reason = "TAKE_PROFIT"
                # Turtle Exit: 10-bar 安値をブレイク
                else:
                    recent_low = lows[i - lookback_exit:i].min()
                    if current_low <= recent_low:
                        exit_price = recent_low
                        reason = "TURTLE_EXIT"
            else:  # short
                if current_high >= entry_price + sl_dist:
                    exit_price = entry_price + sl_dist
                    reason = "STOP_LOSS"
                elif config.use_tp and current_low <= entry_price - tp_dist:
                    exit_price = entry_price - tp_dist
                    reason = "TAKE_PROFIT"
                else:
                    recent_high = highs[i - lookback_exit:i].max()
                    if current_high >= recent_high:
                        exit_price = recent_high
                        reason = "TURTLE_EXIT"

            if exit_price is not None:
                direction = 1 if position == 1 else -1
                gross_pnl = direction * (exit_price - entry_price) * position_size
                fee = (entry_price + exit_price) * position_size * (config.commission_rate + config.slippage_rate)
                net_pnl = gross_pnl - fee
                capital += net_pnl

                trades.append({
                    "entry_date": str(df.index[entry_idx]),
                    "exit_date": str(df.index[i]),
                    "side": "L" if direction == 1 else "S",
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "size": round(position_size, 4),
                    "pnl": round(net_pnl, 2),
                    "reason": reason,
                    "hold_bars": i - entry_idx,
                })

                position = 0
                entry_idx = -1
                cooldown_until_idx = i + config.cooldown_bars

        # 新規エントリー
        if position == 0 and i > cooldown_until_idx:
            atr_pct = current_atr / current_close
            if atr_pct < config.min_atr_pct:
                continue

            # Turtle 20バー高値/安値 (現在バーを除く)
            lookback_high = highs[i - lookback_entry:i].max()
            lookback_low = lows[i - lookback_entry:i].min()

            # Long breakout
            if current_high >= lookback_high and current_close > lookback_high * 0.999:
                stop_dist = current_atr * config.sl_atr_mult
                position_size = (capital * config.risk_per_trade) / stop_dist
                position = 1
                entry_price = current_close * (1 + config.slippage_rate)
                entry_atr = current_atr
                entry_idx = i
            elif current_low <= lookback_low and current_close < lookback_low * 1.001:
                stop_dist = current_atr * config.sl_atr_mult
                position_size = (capital * config.risk_per_trade) / stop_dist
                position = -1
                entry_price = current_close * (1 - config.slippage_rate)
                entry_atr = current_atr
                entry_idx = i

    # 最終クローズ
    if position != 0:
        final_price = closes[-1]
        direction = 1 if position == 1 else -1
        net_pnl = direction * (final_price - entry_price) * position_size
        trades.append({
            "entry_date": str(df.index[entry_idx]),
            "exit_date": str(df.index[-1]),
            "side": "L" if direction == 1 else "S",
            "entry_price": round(entry_price, 4),
            "exit_price": round(final_price, 4),
            "size": round(position_size, 4),
            "pnl": round(net_pnl, 2),
            "reason": "FINAL_CLOSE",
            "hold_bars": len(df) - 1 - entry_idx,
        })

    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = n - wins
    total_pnl = sum(t["pnl"] for t in trades)
    wr = wins / n * 100 if n > 0 else 0
    reasons = Counter(t["reason"] for t in trades)

    return {
        "symbol": symbol,
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 2),
        "total_pnl": round(total_pnl, 2),
        "reasons": dict(reasons),
        "trade_list": trades,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out", default="mtt_4h_results.json")
    args = parser.parse_args()

    config = Config4H()

    print("=" * 90)
    print(f"  MTT 4h Trend Following (Turtle風)")
    print(f"  対象: {SYMBOLS}")
    print(f"  期間: {args.days}日 / 4h足")
    print(f"  Entry: 20-bar high/low breakout")
    print(f"  Exit: 10-bar reverse / SL 2ATR / TP 3ATR")
    print("=" * 90)

    all_trades = []
    symbol_results = []

    for symbol in SYMBOLS:
        print(f"\n{'='*90}\n  {symbol}\n{'='*90}")
        try:
            df = fetch_ccxt_long("bybit", symbol, "4h", args.days)
        except Exception as e:
            print(f"  取得失敗: {e}")
            continue
        if len(df) < 100:
            print(f"  データ不足: {len(df)}")
            continue

        r = backtest_4h(df, config, symbol)
        print(f"  取引: {r['trades']}  勝率: {r['win_rate']:.1f}%  "
              f"PnL: {r['total_pnl']:+,.2f}")
        print(f"  決済内訳: {r['reasons']}")

        symbol_results.append(r)
        all_trades.extend(r["trade_list"])

    # 総合
    print("\n\n" + "=" * 90)
    print("  総合集計")
    print("=" * 90)
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in all_trades)
    overall_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

    print(f"  全取引: {total_trades}")
    print(f"  全勝率: {overall_wr:.1f}%")
    print(f"  全PnL : {total_pnl:+,.2f} USDT")

    # Sub-period
    all_trades_sorted = sorted(all_trades, key=lambda t: t["entry_date"])
    mid = len(all_trades_sorted) // 2
    first_half = sum(t["pnl"] for t in all_trades_sorted[:mid])
    second_half = sum(t["pnl"] for t in all_trades_sorted[mid:])

    pos_syms = sum(1 for r in symbol_results if r["total_pnl"] > 0)

    print("\n  === 新3原則判定 ===")
    c1 = total_trades >= 100
    c2 = total_pnl > 0
    c3 = first_half > 0 and second_half > 0 if total_trades >= 20 else False
    c4 = pos_syms >= (len(symbol_results) + 1) // 2

    print(f"  原則①: 100取引以上         → {'✓' if c1 else '✗'} ({total_trades})")
    print(f"  原則②: 正期待値            → {'✓' if c2 else '✗'} ({total_pnl:+,.2f})")
    print(f"  原則③: 前後半両方黒字      → {'✓' if c3 else '✗'} "
          f"(前{first_half:+,.2f} / 後{second_half:+,.2f})")
    print(f"  原則④: 過半数シンボル黒字  → {'✓' if c4 else '✗'} "
          f"({pos_syms}/{len(symbol_results)})")

    all_pass = c1 and c2 and c3 and c4
    print(f"\n  {'◎ 合格' if all_pass else '✗ 不合格'}")

    with open(PROJECT_ROOT / args.out, "w") as f:
        json.dump({
            "config": asdict(config),
            "symbol_results": [
                {k: v for k, v in r.items() if k != "trade_list"}
                for r in symbol_results
            ],
            "overall": {
                "total_trades": total_trades,
                "overall_win_rate": round(overall_wr, 2),
                "total_pnl": round(total_pnl, 2),
                "first_half": round(first_half, 2),
                "second_half": round(second_half, 2),
                "positive_symbols": pos_syms,
            },
            "verdict": "PASS" if all_pass else "FAIL",
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n📁 保存: {args.out}")


if __name__ == "__main__":
    main()

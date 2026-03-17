#!/usr/bin/env python3
"""
ATRベース動的SL/TP vs 固定SL/TP のバックテスト比較

問題:
  固定SL=-3%がギャップで貫通し、-22%の損失が発生した。
  ATRベースにすることで銘柄ごとのボラティリティに合わせた
  適切なSL/TPを設定できるかを検証する。

比較パターン:
  A) 固定: SL=-3%, TP1=+3%, TP2=+10%, トレーリング=高値-2%
  B) ATR: SL=-1.5xATR(14), TP1=+2.0xATR(14), TP2=+5.0xATR(14), トレーリング=高値-1.0xATR

テスト銘柄: ペーパートレードで損失を出した日本株ショート銘柄群
"""

import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from dataclasses import dataclass

# ATR計算
def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR(Average True Range)を計算する。"""
    high = df["high"] if "high" in df.columns else df["High"]
    low = df["low"] if "low" in df.columns else df["Low"]
    close = df["close"] if "close" in df.columns else df["Close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def fetch_ohlcv(symbol: str, period: str = "1y") -> pd.DataFrame:
    """yfinanceからOHLCVを取得する。"""
    df = yf.download(symbol, period=period, progress=False)
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.dropna()
    return df


@dataclass
class TradeResult:
    symbol: str
    side: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    reason: str
    atr_pct_at_entry: float  # ATR/price (%)


def simulate_trades(df: pd.DataFrame, symbol: str, side: str,
                    sl_pct: float, tp1_pct: float, tp2_pct: float,
                    trailing_pct: float, force_exit_days: int = 7,
                    use_atr: bool = False, atr_sl_mult: float = 1.5,
                    atr_tp1_mult: float = 2.0, atr_tp2_mult: float = 5.0,
                    atr_trail_mult: float = 1.0) -> list[TradeResult]:
    """
    日次OHLCVでショート/ロングトレードをシミュレーションする。

    月初にエントリーし、SL/TP/トレーリング/強制決済で抜ける。
    次の月初に再エントリーを繰り返す。
    """
    results = []
    atr = calc_atr(df, 14)

    i = 0
    while i < len(df) - 1:
        # 月初でエントリー（簡易: 月が変わったタイミング）
        if i == 0 or df.index[i].month != df.index[i-1].month:
            entry_price = float(df["open"].iloc[i])
            entry_date = df.index[i]
            entry_atr = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else None

            if entry_atr is None or entry_price == 0:
                i += 1
                continue

            atr_pct = entry_atr / entry_price  # ATR as % of price

            # ATRベースの場合、銘柄ごとに閾値を計算
            if use_atr:
                eff_sl = -atr_sl_mult * atr_pct
                eff_tp1 = atr_tp1_mult * atr_pct
                eff_tp2 = atr_tp2_mult * atr_pct
                eff_trail = atr_trail_mult * atr_pct
            else:
                eff_sl = sl_pct
                eff_tp1 = tp1_pct
                eff_tp2 = tp2_pct
                eff_trail = trailing_pct

            # ポジションシミュレーション
            peak = entry_price  # ロング: 高値追跡
            trough = entry_price  # ショート: 安値追跡
            tp_stage = 0
            remaining_shares = 1.0  # 正規化: 1.0 = 全量
            total_realized = 0.0
            holding_days = 0
            exit_price = None
            exit_date = None
            reason = None

            for j in range(i, min(i + 60, len(df))):  # 最大60日
                current_high = float(df["high"].iloc[j])
                current_low = float(df["low"].iloc[j])
                current_close = float(df["close"].iloc[j])
                holding_days = (df.index[j] - entry_date).days

                if side == "short":
                    pnl_pct_now = (entry_price - current_close) / entry_price
                    # ショートの最悪ケース: 日中高値で判定
                    worst_pnl = (entry_price - current_high) / entry_price
                    # トレーリング用
                    trough = min(trough, current_low)
                    drawup_from_trough = (current_close - trough) / trough if trough > 0 else 0
                else:
                    pnl_pct_now = (current_close - entry_price) / entry_price
                    worst_pnl = (current_low - entry_price) / entry_price
                    peak = max(peak, current_high)
                    drawdown_from_peak = (current_close - peak) / peak if peak > 0 else 0

                # 1. SL判定（日中の最悪価格で判定）
                if worst_pnl <= eff_sl:
                    # ギャップがあっても日中安値(高値)で執行
                    if side == "short":
                        exit_price = entry_price * (1 - eff_sl)  # SLレベルで約定想定
                        # ただし実際にはギャップで超える可能性
                        exit_price = max(exit_price, current_high)  # 最悪でも日中高値
                        exit_price = min(exit_price, current_high)
                    else:
                        exit_price = min(entry_price * (1 + eff_sl), current_low)
                        exit_price = max(exit_price, current_low)
                    exit_date = df.index[j]
                    reason = "STOP_LOSS"
                    total_realized = ((entry_price - exit_price) / entry_price if side == "short"
                                      else (exit_price - entry_price) / entry_price) * remaining_shares
                    remaining_shares = 0
                    break

                # 2. 強制決済
                if holding_days >= force_exit_days:
                    exit_price = current_close
                    exit_date = df.index[j]
                    reason = "FORCE_EXIT"
                    total_realized = pnl_pct_now * remaining_shares
                    remaining_shares = 0
                    break

                # 3. トレーリングストップ（TP1以降）
                if tp_stage >= 1:
                    if side == "short" and drawup_from_trough >= eff_trail:
                        exit_price = current_close
                        exit_date = df.index[j]
                        reason = "TRAILING_STOP"
                        total_realized += pnl_pct_now * remaining_shares
                        remaining_shares = 0
                        break
                    elif side == "long" and drawdown_from_peak <= -eff_trail:
                        exit_price = current_close
                        exit_date = df.index[j]
                        reason = "TRAILING_STOP"
                        total_realized += pnl_pct_now * remaining_shares
                        remaining_shares = 0
                        break

                # 4. TP1（1/2利確）
                if tp_stage == 0 and pnl_pct_now >= eff_tp1:
                    sell_shares = remaining_shares / 2
                    total_realized += pnl_pct_now * sell_shares
                    remaining_shares -= sell_shares
                    tp_stage = 1

                # 5. TP2（1/2利確）
                if tp_stage == 1 and pnl_pct_now >= eff_tp2:
                    sell_shares = remaining_shares / 2
                    total_realized += pnl_pct_now * sell_shares
                    remaining_shares -= sell_shares
                    tp_stage = 2

            # ループ終了（60日到達 or 期間終了）
            if remaining_shares > 0:
                if exit_price is None:
                    exit_price = float(df["close"].iloc[min(i + 59, len(df) - 1)])
                    exit_date = df.index[min(i + 59, len(df) - 1)]
                    reason = reason or "PERIOD_END"
                    if side == "short":
                        final_pnl = (entry_price - exit_price) / entry_price
                    else:
                        final_pnl = (exit_price - entry_price) / entry_price
                    total_realized += final_pnl * remaining_shares

            results.append(TradeResult(
                symbol=symbol,
                side=side,
                entry_date=str(entry_date.date()),
                exit_date=str(exit_date.date()) if exit_date is not None else "N/A",
                entry_price=entry_price,
                exit_price=exit_price if exit_price is not None else 0,
                pnl_pct=total_realized * 100,
                reason=reason or "UNKNOWN",
                atr_pct_at_entry=atr_pct * 100,
            ))

            # 次のエントリーポイントまでスキップ
            if exit_date is not None:
                while i < len(df) and df.index[i] <= exit_date:
                    i += 1
            else:
                i += 60
        else:
            i += 1

    return results


def simulate_trades_capped(df: pd.DataFrame, symbol: str, side: str,
                            atr_sl_mult: float = 1.5, atr_tp1_mult: float = 2.0,
                            atr_tp2_mult: float = 5.0, atr_trail_mult: float = 1.0,
                            sl_cap: float = -0.08, tp1_floor: float = 0.02,
                            force_exit_days: int = 7) -> list[TradeResult]:
    """
    C) ハイブリッド方式: ATRベース + 上限/下限キャップ
    SL: max(固定下限, -1.5xATR)  つまりATRが大きすぎるときは-8%で切る
    TP1: max(固定下限, 2.0xATR)  ATRが小さすぎるときは2%を最低保証
    """
    results = []
    atr = calc_atr(df, 14)

    i = 0
    while i < len(df) - 1:
        if i == 0 or df.index[i].month != df.index[i-1].month:
            entry_price = float(df["open"].iloc[i])
            entry_date = df.index[i]
            entry_atr = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else None

            if entry_atr is None or entry_price == 0:
                i += 1
                continue

            atr_pct = entry_atr / entry_price

            # ATRベース + キャップ
            eff_sl = max(sl_cap, -atr_sl_mult * atr_pct)  # -8%が下限
            eff_tp1 = max(tp1_floor, atr_tp1_mult * atr_pct)  # 2%が下限
            eff_tp2 = max(0.06, atr_tp2_mult * atr_pct)  # 6%が下限
            eff_trail = max(0.015, atr_trail_mult * atr_pct)  # 1.5%が下限

            peak = entry_price
            trough = entry_price
            tp_stage = 0
            remaining_shares = 1.0
            total_realized = 0.0
            holding_days = 0
            exit_price = None
            exit_date = None
            reason = None

            for j in range(i, min(i + 60, len(df))):
                current_high = float(df["high"].iloc[j])
                current_low = float(df["low"].iloc[j])
                current_close = float(df["close"].iloc[j])
                holding_days = (df.index[j] - entry_date).days

                if side == "short":
                    pnl_pct_now = (entry_price - current_close) / entry_price
                    worst_pnl = (entry_price - current_high) / entry_price
                    trough = min(trough, current_low)
                    drawup_from_trough = (current_close - trough) / trough if trough > 0 else 0
                else:
                    pnl_pct_now = (current_close - entry_price) / entry_price
                    worst_pnl = (current_low - entry_price) / entry_price
                    peak = max(peak, current_high)
                    drawdown_from_peak = (current_close - peak) / peak if peak > 0 else 0

                if worst_pnl <= eff_sl:
                    if side == "short":
                        exit_price = max(entry_price * (1 - eff_sl), current_high)
                        exit_price = min(exit_price, current_high)
                    else:
                        exit_price = min(entry_price * (1 + eff_sl), current_low)
                        exit_price = max(exit_price, current_low)
                    exit_date = df.index[j]
                    reason = "STOP_LOSS"
                    total_realized = ((entry_price - exit_price) / entry_price if side == "short"
                                      else (exit_price - entry_price) / entry_price) * remaining_shares
                    remaining_shares = 0
                    break

                if holding_days >= force_exit_days:
                    exit_price = current_close
                    exit_date = df.index[j]
                    reason = "FORCE_EXIT"
                    total_realized = pnl_pct_now * remaining_shares
                    remaining_shares = 0
                    break

                if tp_stage >= 1:
                    if side == "short" and drawup_from_trough >= eff_trail:
                        exit_price = current_close
                        exit_date = df.index[j]
                        reason = "TRAILING_STOP"
                        total_realized += pnl_pct_now * remaining_shares
                        remaining_shares = 0
                        break
                    elif side == "long" and drawdown_from_peak <= -eff_trail:
                        exit_price = current_close
                        exit_date = df.index[j]
                        reason = "TRAILING_STOP"
                        total_realized += pnl_pct_now * remaining_shares
                        remaining_shares = 0
                        break

                if tp_stage == 0 and pnl_pct_now >= eff_tp1:
                    sell_shares = remaining_shares / 2
                    total_realized += pnl_pct_now * sell_shares
                    remaining_shares -= sell_shares
                    tp_stage = 1

                if tp_stage == 1 and pnl_pct_now >= eff_tp2:
                    sell_shares = remaining_shares / 2
                    total_realized += pnl_pct_now * sell_shares
                    remaining_shares -= sell_shares
                    tp_stage = 2

            if remaining_shares > 0:
                if exit_price is None:
                    exit_price = float(df["close"].iloc[min(i + 59, len(df) - 1)])
                    exit_date = df.index[min(i + 59, len(df) - 1)]
                    reason = reason or "PERIOD_END"
                    if side == "short":
                        final_pnl = (entry_price - exit_price) / entry_price
                    else:
                        final_pnl = (exit_price - entry_price) / entry_price
                    total_realized += final_pnl * remaining_shares

            results.append(TradeResult(
                symbol=symbol, side=side,
                entry_date=str(entry_date.date()),
                exit_date=str(exit_date.date()) if exit_date is not None else "N/A",
                entry_price=entry_price,
                exit_price=exit_price if exit_price is not None else 0,
                pnl_pct=total_realized * 100,
                reason=reason or "UNKNOWN",
                atr_pct_at_entry=atr_pct * 100,
            ))

            if exit_date is not None:
                while i < len(df) and df.index[i] <= exit_date:
                    i += 1
            else:
                i += 60
        else:
            i += 1

    return results


def run_comparison():
    """固定 vs ATRベース vs ハイブリッド のバックテスト比較を実行する。"""

    # テスト対象銘柄（ペーパートレードで損失/運用中の日本株ショート銘柄）
    test_symbols = {
        # 損失銘柄
        "6613.T": "QDレーザ",
        "6387.T": "サムコ",
        # 現行ポジション
        "5572.T": "Ridge-i",
        "9412.T": "スカパーJSAT",
        "3315.T": "日本コークス",
        "7119.T": "ハルメクHD",
        "2293.T": "滝沢ハム",
        "2818.T": "ピエトロ",
        "6454.T": "マックス",
        "7375.T": "リファインバース",
    }

    print("=" * 80)
    print("ATRベース動的SL/TP vs 固定SL/TP バックテスト比較")
    print("=" * 80)

    all_fixed = []
    all_atr = []
    all_hybrid = []

    for symbol, name in test_symbols.items():
        print(f"\n--- {name} ({symbol}) ---")
        df = fetch_ohlcv(symbol, period="2y")
        if df is None or len(df) < 30:
            print(f"  データ不足: スキップ")
            continue

        atr = calc_atr(df, 14)
        latest_atr_pct = float(atr.iloc[-1] / df["close"].iloc[-1] * 100)
        print(f"  データ: {len(df)}日分, 最新ATR%: {latest_atr_pct:.2f}%")

        # A) 固定パラメータ
        fixed_results = simulate_trades(
            df, symbol, side="short",
            sl_pct=-0.03, tp1_pct=0.03, tp2_pct=0.10, trailing_pct=0.02,
            use_atr=False,
        )
        all_fixed.extend(fixed_results)

        # B) ATRベース（キャップなし）
        atr_results = simulate_trades(
            df, symbol, side="short",
            sl_pct=-0.03, tp1_pct=0.03, tp2_pct=0.10, trailing_pct=0.02,
            use_atr=True,
            atr_sl_mult=1.5, atr_tp1_mult=2.0, atr_tp2_mult=5.0, atr_trail_mult=1.0,
        )
        all_atr.extend(atr_results)

        # C) ハイブリッド（ATR + キャップ）
        hybrid_results = simulate_trades_capped(
            df, symbol, side="short",
            atr_sl_mult=1.5, atr_tp1_mult=2.0, atr_tp2_mult=5.0, atr_trail_mult=1.0,
            sl_cap=-0.08, tp1_floor=0.02,
        )
        all_hybrid.extend(hybrid_results)

        # 銘柄ごとの比較サマリー
        for label, res in [("固定", fixed_results), ("ATR", atr_results), ("混成", hybrid_results)]:
            if res:
                avg = np.mean([r.pnl_pct for r in res])
                wins = sum(1 for r in res if r.pnl_pct > 0)
                worst = min(r.pnl_pct for r in res)
                print(f"  [{label}] {len(res)}回, 平均: {avg:+.2f}%, "
                      f"勝率: {wins}/{len(res)}, 最大損失: {worst:+.2f}%")

    # === 全体集計 ===
    print("\n" + "=" * 80)
    print("全体集計（A:固定 / B:ATR / C:ハイブリッド）")
    print("=" * 80)

    for label, results in [("A) 固定SL/TP", all_fixed),
                           ("B) ATRベースSL/TP", all_atr),
                           ("C) ハイブリッド(ATR+キャップ)", all_hybrid)]:
        if not results:
            continue
        pnls = [r.pnl_pct for r in results]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        avg_win = np.mean([p for p in pnls if p > 0]) if wins > 0 else 0
        avg_loss = np.mean([p for p in pnls if p <= 0]) if losses > 0 else 0

        print(f"\n[{label}]")
        print(f"  トレード数: {len(results)}")
        print(f"  勝率: {wins}/{len(results)} ({wins/len(results)*100:.1f}%)")
        print(f"  平均PnL: {np.mean(pnls):+.2f}%")
        print(f"  平均利益: {avg_win:+.2f}%  平均損失: {avg_loss:+.2f}%")
        print(f"  RR比: {abs(avg_win/avg_loss) if avg_loss != 0 else 0:.2f}")
        print(f"  累積PnL: {sum(pnls):+.2f}%")
        print(f"  最大利益: {max(pnls):+.2f}%  最大損失: {min(pnls):+.2f}%")

        sl_count = sum(1 for r in results if r.reason == "STOP_LOSS")
        tp_count = sum(1 for r in results if "TAKE_PROFIT" in r.reason)
        trail_count = sum(1 for r in results if "TRAILING" in r.reason)
        force_count = sum(1 for r in results if r.reason == "FORCE_EXIT")
        print(f"  決済理由: SL={sl_count}, TP={tp_count}, Trail={trail_count}, Force={force_count}")


if __name__ == "__main__":
    run_comparison()

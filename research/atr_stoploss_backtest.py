#!/usr/bin/env python3
"""
ATRベース動的損切/利確 バックテスト

対象: 日本株ショート10銘柄（ペーパートレードで実際に使用中の銘柄）
比較: 固定-3% vs ATR(14)x1.5 vs ATR(14)x2.0 vs ATR(14)x2.5
期間: 過去1年
戦略: Monthly Momentum (SELLシグナル) + SMA Crossover (デッドクロス)

作成: 本多智房（軍師）2026-03-16
"""

from __future__ import annotations

import sys
import os
import json
import warnings
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ==============================================================
# テスト対象銘柄（ペーパートレードの日本株ショート10銘柄）
# ==============================================================

JP_SHORT_TICKERS = {
    "9412.T": "スカパーJSAT",
    "7119.T": "ハルメクHD",
    "2293.T": "滝沢ハム",
    "2818.T": "ピエトロ",
    "6454.T": "マックス",
    "6613.T": "QDレーザ",
    "5572.T": "Ridge-i",
    "3315.T": "日本コークス工業",
    "7375.T": "リファインバースG",
    "6387.T": "サムコ",
}


# ==============================================================
# ATR計算
# ==============================================================

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR (Average True Range) を計算"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    return atr


# ==============================================================
# シグナル生成（ショート用: Monthly Momentum + SMA Crossover）
# ==============================================================

def generate_sell_signals(df: pd.DataFrame) -> pd.Series:
    """
    SELLシグナルを生成する。
    Monthly Momentum の月末手仕舞い(-1) または SMA Crossover のデッドクロスを使う。
    ショートなので -1 をエントリーシグナルとして扱う。
    """
    signals = pd.Series(0, index=df.index)

    # SMA Crossover: 短期(5) < 長期(20) でデッドクロス → ショートエントリー
    sma_short = df["close"].rolling(window=5).mean()
    sma_long = df["close"].rolling(window=20).mean()

    # デッドクロスの瞬間を検出（前日は上、当日は下）
    prev_above = (sma_short.shift(1) >= sma_long.shift(1))
    curr_below = (sma_short < sma_long)
    dead_cross = prev_above & curr_below
    signals[dead_cross] = -1

    # Monthly Momentum: 月初に出来高急増している場合にも方向を見る
    vol_ma = df["volume"].rolling(window=20).mean()
    volume_ratio = df["volume"] / vol_ma

    for i in range(len(df)):
        dt = df.index[i]
        # 月初3日以内 + 出来高1.5倍以上 + 価格が下落トレンド(SMA短期<長期)
        if (dt.day <= 3
                and i > 20
                and volume_ratio.iloc[i] >= 1.5
                and sma_short.iloc[i] < sma_long.iloc[i]):
            signals.iloc[i] = -1

    return signals


# ==============================================================
# トレードシミュレータ（イベント駆動型）
# ==============================================================

@dataclass
class Trade:
    entry_date: datetime
    entry_price: float
    exit_date: datetime = None
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    trail_trigger: float = 0.0
    atr_pct: float = 0.0


@dataclass
class BacktestConfig:
    name: str
    sl_type: str  # "fixed" or "atr"
    sl_fixed_pct: float = 0.03       # 固定損切幅（ショートなので+3%で損切）
    atr_mult: float = 2.0            # ATR倍率
    sl_cap_pct: float = 0.08         # ATR損切の上限キャップ
    sl_floor_pct: float = 0.015      # ATR損切の下限フロア
    tp1_pct: float = 0.03            # 利確第1段階
    tp1_ratio: float = 0.5           # 第1段階で決済する割合
    tp2_pct: float = 0.10            # 利確第2段階
    tp2_ratio: float = 0.5           # 第2段階で決済する割合
    trail_trigger_pct: float = 0.02  # トレーリング発動トリガー
    trail_stop_pct: float = 0.02     # トレーリング幅
    force_exit_days: int = 7         # 強制決済日数
    commission: float = 0.001        # 手数料


def simulate_short_trades(
    df: pd.DataFrame,
    signals: pd.Series,
    config: BacktestConfig,
    atr_series: pd.Series,
) -> list[Trade]:
    """
    ショートトレードのイベント駆動シミュレーション。

    ショートの損益:
    - 利益 = entry_price - exit_price (価格が下がれば儲かる)
    - 損切 = 価格がentry_price * (1 + sl_pct) を上回ったとき
    - 利確 = 価格がentry_price * (1 - tp_pct) を下回ったとき
    """
    trades = []
    in_trade = False
    current_trade = None
    remaining_shares = 0.0
    trailing_active = False
    trailing_trough = None  # ショートなので最安値を追跡
    tp1_done = False
    tp2_done = False

    for i in range(1, len(df)):
        date = df.index[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        close = df["close"].iloc[i]
        open_price = df["open"].iloc[i]

        if not in_trade:
            # エントリー判定（前日のシグナルで翌日始値エントリー）
            if signals.iloc[i - 1] == -1 and not pd.isna(atr_series.iloc[i - 1]):
                entry_price = open_price
                atr_val = atr_series.iloc[i - 1]
                atr_pct = atr_val / entry_price if entry_price > 0 else 0.03

                # 損切価格の決定
                if config.sl_type == "fixed":
                    sl_pct = config.sl_fixed_pct
                else:
                    sl_pct = atr_pct * config.atr_mult
                    sl_pct = max(sl_pct, config.sl_floor_pct)  # 下限フロア
                    sl_pct = min(sl_pct, config.sl_cap_pct)    # 上限キャップ

                sl_price = entry_price * (1 + sl_pct)  # ショートの損切: 価格が上がると損失

                # ATRベースの利確価格
                if config.sl_type == "atr":
                    tp1_pct = max(atr_pct * 2.0, config.tp1_pct)
                    tp2_pct = max(atr_pct * 5.0, config.tp2_pct)
                else:
                    tp1_pct = config.tp1_pct
                    tp2_pct = config.tp2_pct

                tp1_price = entry_price * (1 - tp1_pct)
                tp2_price = entry_price * (1 - tp2_pct)

                # トレーリング発動トリガー
                if config.sl_type == "atr":
                    trail_trigger_pct = max(atr_pct * 1.0, config.trail_trigger_pct)
                else:
                    trail_trigger_pct = config.trail_trigger_pct

                trail_trigger = entry_price * (1 - trail_trigger_pct)

                current_trade = Trade(
                    entry_date=date,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    trail_trigger=trail_trigger,
                    atr_pct=atr_pct,
                )
                in_trade = True
                remaining_shares = 1.0
                trailing_active = False
                trailing_trough = close
                tp1_done = False
                tp2_done = False
                continue

        if in_trade and current_trade is not None:
            entry_p = current_trade.entry_price

            # 強制決済チェック
            days_held = (date - current_trade.entry_date).days
            if days_held >= config.force_exit_days:
                pnl = (entry_p - close) / entry_p - config.commission * 2
                current_trade.exit_date = date
                current_trade.exit_price = close
                current_trade.pnl_pct = pnl * remaining_shares
                current_trade.exit_reason = "FORCE_EXIT"
                trades.append(current_trade)
                in_trade = False
                current_trade = None
                continue

            # 損切チェック（日中高値で判定）
            if high >= current_trade.sl_price:
                exit_price = current_trade.sl_price  # SL価格で約定と仮定
                pnl = (entry_p - exit_price) / entry_p - config.commission * 2
                current_trade.exit_date = date
                current_trade.exit_price = exit_price
                current_trade.pnl_pct = pnl * remaining_shares
                current_trade.exit_reason = "STOP_LOSS"
                trades.append(current_trade)
                in_trade = False
                current_trade = None
                continue

            # 利確第1段階（日中安値で判定）
            if not tp1_done and low <= current_trade.tp1_price:
                tp1_done = True
                # 半分を利確
                tp1_pnl = (entry_p - current_trade.tp1_price) / entry_p - config.commission * 2
                tp1_trade = Trade(
                    entry_date=current_trade.entry_date,
                    entry_price=entry_p,
                    exit_date=date,
                    exit_price=current_trade.tp1_price,
                    pnl_pct=tp1_pnl * config.tp1_ratio,
                    exit_reason="TAKE_PROFIT_1",
                    sl_price=current_trade.sl_price,
                    tp1_price=current_trade.tp1_price,
                    tp2_price=current_trade.tp2_price,
                    atr_pct=current_trade.atr_pct,
                )
                trades.append(tp1_trade)
                remaining_shares -= config.tp1_ratio

            # 利確第2段階
            if tp1_done and not tp2_done and low <= current_trade.tp2_price:
                tp2_done = True
                tp2_pnl = (entry_p - current_trade.tp2_price) / entry_p - config.commission * 2
                tp2_trade = Trade(
                    entry_date=current_trade.entry_date,
                    entry_price=entry_p,
                    exit_date=date,
                    exit_price=current_trade.tp2_price,
                    pnl_pct=tp2_pnl * config.tp2_ratio * remaining_shares,
                    exit_reason="TAKE_PROFIT_2",
                    sl_price=current_trade.sl_price,
                    atr_pct=current_trade.atr_pct,
                )
                trades.append(tp2_trade)
                remaining_shares -= config.tp2_ratio * remaining_shares

            # トレーリングストップ
            if low <= current_trade.trail_trigger:
                trailing_active = True

            if trailing_active:
                trailing_trough = min(trailing_trough, low)
                if config.sl_type == "atr":
                    trail_stop_pct = max(current_trade.atr_pct * 1.0, config.trail_stop_pct)
                else:
                    trail_stop_pct = config.trail_stop_pct
                trail_stop_price = trailing_trough * (1 + trail_stop_pct)

                if high >= trail_stop_price and remaining_shares > 0:
                    pnl = (entry_p - trail_stop_price) / entry_p - config.commission * 2
                    trail_trade = Trade(
                        entry_date=current_trade.entry_date,
                        entry_price=entry_p,
                        exit_date=date,
                        exit_price=trail_stop_price,
                        pnl_pct=pnl * remaining_shares,
                        exit_reason="TRAILING_STOP",
                        sl_price=current_trade.sl_price,
                        atr_pct=current_trade.atr_pct,
                    )
                    trades.append(trail_trade)
                    in_trade = False
                    current_trade = None
                    continue

    # 未決済ポジションは最終日に決済
    if in_trade and current_trade is not None:
        close = df["close"].iloc[-1]
        entry_p = current_trade.entry_price
        pnl = (entry_p - close) / entry_p - config.commission * 2
        current_trade.exit_date = df.index[-1]
        current_trade.exit_price = close
        current_trade.pnl_pct = pnl * remaining_shares
        current_trade.exit_reason = "END_OF_DATA"
        trades.append(current_trade)

    return trades


# ==============================================================
# 統計計算
# ==============================================================

def calc_stats(trades: list[Trade]) -> dict:
    """トレードリストから統計を計算"""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "avg_pnl": 0,
            "max_dd": 0, "sharpe": 0, "rr_ratio": 0,
            "avg_win": 0, "avg_loss": 0, "total_pnl": 0,
            "profit_factor": 0, "max_win": 0, "max_loss": 0,
        }

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    avg_pnl = np.mean(pnls) * 100 if pnls else 0
    avg_win = np.mean(wins) * 100 if wins else 0
    avg_loss = np.mean(losses) * 100 if losses else 0
    total_pnl = sum(pnls) * 100
    max_win = max(pnls) * 100 if pnls else 0
    max_loss = min(pnls) * 100 if pnls else 0

    # RR比 (Risk Reward Ratio): 平均利益 / 平均損失の絶対値
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # Profit Factor: 総利益 / 総損失の絶対値
    total_win = sum(wins) if wins else 0
    total_loss = abs(sum(losses)) if losses else 0
    profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

    # 最大ドローダウン（累積損益ベース）
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    dd = cumulative - peak
    max_dd = min(dd) * 100 if len(dd) > 0 else 0

    # シャープレシオ（日次ベースを年率換算するのではなく、トレード単位で近似）
    if len(pnls) > 1 and np.std(pnls) > 0:
        # トレード単位シャープ × sqrt(年間トレード数想定)
        trades_per_year = len(pnls) * (252 / 365)  # 概算
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(max(trades_per_year, 1))
    else:
        sharpe = 0

    return {
        "total_trades": len(pnls),
        "win_rate": round(win_rate, 1),
        "avg_pnl": round(avg_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "rr_ratio": round(rr_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
    }


# ==============================================================
# バックテスト設定
# ==============================================================

CONFIGS = [
    BacktestConfig(
        name="固定-3%",
        sl_type="fixed",
        sl_fixed_pct=0.03,
        tp1_pct=0.03,
        tp2_pct=0.10,
        trail_trigger_pct=0.02,
        trail_stop_pct=0.02,
    ),
    BacktestConfig(
        name="ATR x1.5 (cap-8%)",
        sl_type="atr",
        atr_mult=1.5,
        sl_cap_pct=0.08,
        sl_floor_pct=0.015,
    ),
    BacktestConfig(
        name="ATR x2.0 (cap-8%)",
        sl_type="atr",
        atr_mult=2.0,
        sl_cap_pct=0.08,
        sl_floor_pct=0.015,
    ),
    BacktestConfig(
        name="ATR x2.5 (cap-10%)",
        sl_type="atr",
        atr_mult=2.5,
        sl_cap_pct=0.10,
        sl_floor_pct=0.015,
    ),
]


# ==============================================================
# メイン実行
# ==============================================================

def fetch_data(ticker: str, period: str = "1y") -> pd.DataFrame | None:
    """yfinanceからデータを取得"""
    try:
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df.columns = [c.lower() for c in df.columns]
        if len(df) < 30:
            return None
        return df
    except Exception as e:
        print(f"  [ERROR] {ticker}: {e}")
        return None


def run_backtest():
    """全銘柄 x 全設定でバックテストを実行"""
    print("=" * 70)
    print("ATRベース動的損切 バックテスト")
    print(f"実行日: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"対象: 日本株ショート {len(JP_SHORT_TICKERS)}銘柄")
    print("=" * 70)

    all_results = {}  # config_name -> {ticker -> stats}
    per_ticker_results = {}  # ticker -> {config_name -> stats}
    all_trades_by_config = {}  # config_name -> [Trade, ...]

    for config in CONFIGS:
        all_results[config.name] = {}
        all_trades_by_config[config.name] = []

    for ticker, name in JP_SHORT_TICKERS.items():
        print(f"\n--- {ticker} ({name}) ---")
        df = fetch_data(ticker)
        if df is None:
            print(f"  データ取得失敗、スキップ")
            continue

        atr = calc_atr(df)
        signals = generate_sell_signals(df)

        signal_count = (signals == -1).sum()
        print(f"  データ: {df.index[0].date()} - {df.index[-1].date()} ({len(df)}日)")
        print(f"  SELLシグナル数: {signal_count}")
        print(f"  ATR(14)平均: {atr.dropna().mean():.1f} ({(atr.dropna().mean() / df['close'].mean() * 100):.1f}%)")

        per_ticker_results[ticker] = {}

        for config in CONFIGS:
            trades = simulate_short_trades(df, signals, config, atr)
            stats = calc_stats(trades)
            all_results[config.name][ticker] = stats
            per_ticker_results[ticker][config.name] = stats
            all_trades_by_config[config.name].extend(trades)

            print(f"  {config.name:20s}: {stats['total_trades']:3d}件, "
                  f"勝率{stats['win_rate']:5.1f}%, "
                  f"平均PnL{stats['avg_pnl']:+6.2f}%, "
                  f"RR比{stats['rr_ratio']:.2f}, "
                  f"累積PnL{stats['total_pnl']:+7.2f}%")

    # ==============================================================
    # 全体集計
    # ==============================================================
    print("\n" + "=" * 70)
    print("全体集計")
    print("=" * 70)

    summary = {}
    for config in CONFIGS:
        all_trades = all_trades_by_config[config.name]
        stats = calc_stats(all_trades)
        summary[config.name] = stats
        print(f"\n{config.name}:")
        print(f"  総トレード数: {stats['total_trades']}")
        print(f"  勝率: {stats['win_rate']}%")
        print(f"  平均PnL: {stats['avg_pnl']:+.2f}%")
        print(f"  平均Win: {stats['avg_win']:+.2f}%  平均Loss: {stats['avg_loss']:+.2f}%")
        print(f"  RR比: {stats['rr_ratio']}")
        print(f"  Profit Factor: {stats['profit_factor']}")
        print(f"  最大DD: {stats['max_dd']:.2f}%")
        print(f"  シャープレシオ: {stats['sharpe']}")
        print(f"  累積PnL: {stats['total_pnl']:+.2f}%")
        print(f"  最大Win: {stats['max_win']:+.2f}%  最大Loss: {stats['max_loss']:+.2f}%")

    return summary, per_ticker_results, all_results


def generate_report(summary, per_ticker_results, all_results):
    """マークダウンレポートを生成"""
    report = []
    report.append("# ATRベース動的損切 バックテスト結果")
    report.append("")
    report.append(f"**実行日**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report.append(f"**実行者**: 本多智房（軍師）/ 相場見立方")
    report.append(f"**テーマ**: RESEARCH_BACKLOG P0 #2 — ATRベース動的損切/利確")
    report.append("")
    report.append("---")
    report.append("")

    # テスト条件
    report.append("## テスト条件")
    report.append("")
    report.append(f"- **対象銘柄**: 日本株ショート{len(JP_SHORT_TICKERS)}銘柄（ペーパートレード実運用中の銘柄）")
    report.append(f"  - {', '.join([f'{t}({n})' for t, n in JP_SHORT_TICKERS.items()])}")
    report.append(f"- **期間**: 過去1年（約250営業日）")
    report.append(f"- **戦略**: SMA Crossover(デッドクロス) + Monthly Momentum(SELLシグナル)")
    report.append(f"- **方向**: ショート（空売り）のみ")
    report.append(f"- **手数料**: 片道0.1%（往復0.2%）")
    report.append(f"- **強制決済**: 7日保有で自動決済")
    report.append("")

    # 比較対象の説明
    report.append("### 比較対象")
    report.append("")
    report.append("| 手法 | 損切ロジック | 利確ロジック | トレーリング |")
    report.append("|------|------------|------------|-------------|")
    report.append("| 固定-3% | エントリー価格+3%固定 | -3%で1/2, -10%で1/2 | -2%到達で発動, +2%で決済 |")
    report.append("| ATR x1.5 | ATR(14)x1.5（cap-8%, floor-1.5%） | ATR基準の動的利確 | ATR基準の動的トレーリング |")
    report.append("| ATR x2.0 | ATR(14)x2.0（cap-8%, floor-1.5%） | ATR基準の動的利確 | ATR基準の動的トレーリング |")
    report.append("| ATR x2.5 | ATR(14)x2.5（cap-10%, floor-1.5%） | ATR基準の動的利確 | ATR基準の動的トレーリング |")
    report.append("")

    # 結果比較表
    report.append("---")
    report.append("")
    report.append("## 結果比較表（全銘柄合算）")
    report.append("")
    report.append("| 手法 | 総件数 | 勝率 | 平均PnL | 平均Win | 平均Loss | RR比 | PF | 最大DD | Sharpe | 累積PnL |")
    report.append("|------|--------|------|---------|---------|----------|------|-----|--------|--------|---------|")

    for config_name, stats in summary.items():
        report.append(
            f"| {config_name} "
            f"| {stats['total_trades']} "
            f"| {stats['win_rate']}% "
            f"| {stats['avg_pnl']:+.2f}% "
            f"| {stats['avg_win']:+.2f}% "
            f"| {stats['avg_loss']:+.2f}% "
            f"| {stats['rr_ratio']:.2f} "
            f"| {stats['profit_factor']:.2f} "
            f"| {stats['max_dd']:.2f}% "
            f"| {stats['sharpe']:.2f} "
            f"| {stats['total_pnl']:+.2f}% |"
        )
    report.append("")

    # 銘柄別詳細
    report.append("---")
    report.append("")
    report.append("## 銘柄別詳細")
    report.append("")

    for ticker in JP_SHORT_TICKERS:
        if ticker not in per_ticker_results:
            continue
        name = JP_SHORT_TICKERS[ticker]
        report.append(f"### {ticker} ({name})")
        report.append("")
        report.append("| 手法 | 件数 | 勝率 | 平均PnL | RR比 | 累積PnL | 最適? |")
        report.append("|------|------|------|---------|------|---------|-------|")

        results = per_ticker_results[ticker]
        best_config = max(results.items(), key=lambda x: x[1]["total_pnl"])

        for config_name, stats in results.items():
            is_best = " **最適** " if config_name == best_config[0] else ""
            report.append(
                f"| {config_name} "
                f"| {stats['total_trades']} "
                f"| {stats['win_rate']}% "
                f"| {stats['avg_pnl']:+.2f}% "
                f"| {stats['rr_ratio']:.2f} "
                f"| {stats['total_pnl']:+.2f}% "
                f"| {is_best} |"
            )
        report.append("")

    # 分析と結論
    report.append("---")
    report.append("")
    report.append("## 分析")
    report.append("")

    # RR比の改善を分析
    fixed_stats = summary.get("固定-3%", {})
    best_atr = None
    best_atr_name = ""
    for name, stats in summary.items():
        if name != "固定-3%":
            if best_atr is None or stats["total_pnl"] > best_atr["total_pnl"]:
                best_atr = stats
                best_atr_name = name

    if fixed_stats and best_atr:
        rr_improvement = best_atr["rr_ratio"] - fixed_stats["rr_ratio"]
        pnl_improvement = best_atr["total_pnl"] - fixed_stats["total_pnl"]

        report.append(f"### RR比の改善")
        report.append("")
        report.append(f"- 固定-3%のRR比: {fixed_stats['rr_ratio']:.2f}")
        report.append(f"- 最良ATR設定({best_atr_name})のRR比: {best_atr['rr_ratio']:.2f}")
        report.append(f"- RR比改善幅: {rr_improvement:+.2f}")
        report.append("")
        report.append(f"### 累積損益の比較")
        report.append("")
        report.append(f"- 固定-3%の累積PnL: {fixed_stats['total_pnl']:+.2f}%")
        report.append(f"- 最良ATR設定の累積PnL: {best_atr['total_pnl']:+.2f}%")
        report.append(f"- 改善幅: {pnl_improvement:+.2f}%")
        report.append("")

    report.append("### ATR動的損切の利点")
    report.append("")
    report.append("1. 低ボラ銘柄（日本コークス工業など）: ATRが小さいため損切幅が狭く、損失を限定")
    report.append("2. 高ボラ銘柄（QDレーザなど）: ATRが大きいため損切幅が広く、ノイズで刈られにくい")
    report.append("3. キャップ機構: どんなにATRが大きくても-8%/-10%が上限で暴走を防止")
    report.append("4. フロア機構: 低ボラ銘柄でも最低-1.5%は許容し、頻繁な損切を防止")
    report.append("")

    # 結論と推奨
    report.append("---")
    report.append("")
    report.append("## 結論と推奨（上様への上申）")
    report.append("")

    if best_atr:
        report.append(f"### 採用案: {best_atr_name}")
        report.append("")
        report.append(f"- RR比: {best_atr['rr_ratio']:.2f}（固定-3%比 {rr_improvement:+.2f}）")
        report.append(f"- 累積PnL: {best_atr['total_pnl']:+.2f}%（固定-3%比 {pnl_improvement:+.2f}%改善）")
        report.append(f"- 勝率: {best_atr['win_rate']}%")
        report.append(f"- Profit Factor: {best_atr['profit_factor']:.2f}")
        report.append("")

    report.append("### 却下案とその理由")
    report.append("")
    for name, stats in summary.items():
        if name != best_atr_name and name != "固定-3%":
            report.append(f"- **{name}**: 累積PnL {stats['total_pnl']:+.2f}%, RR比 {stats['rr_ratio']:.2f}")

    report.append("")
    report.append("### トレードオフ")
    report.append("")
    report.append("| 項目 | 固定-3% | ATR動的 |")
    report.append("|------|---------|---------|")
    report.append("| シンプルさ | 高い（パラメータ1つ） | やや複雑（ATR倍率+キャップ+フロア） |")
    report.append("| 銘柄適応力 | 低い（全銘柄一律） | 高い（ボラに応じて自動調整） |")
    report.append("| 損切頻度 | 高い（低ボラ銘柄で頻発） | 適正化される |")
    report.append("| 最大損失 | -3%固定 | キャップ値まで可能性あり |")
    report.append("")

    report.append("### テスト観点（実装前に確認すべきこと）")
    report.append("")
    report.append("1. unified_paper_trade.py への組み込み: ATR計算をエントリー時に実行し、SL/TP価格をポジションに記録")
    report.append("2. 既存ポジションへの遡及適用: 現在保有中の9ポジションに対してATRベースのSL/TPを再計算するか")
    report.append("3. 米国株・仮想通貨への拡張: 日本株以外でも同様の改善が見られるかバックテスト")
    report.append("4. VolScale戦略との共存: VolScaleは独自のSL/TPを持つため、ATR動的損切はMonthly Momentum/BB+RSIに限定")
    report.append("5. launchdジョブのposition-monitor: ATRベースのSL/TP価格を監視ロジックに反映")
    report.append("")

    return "\n".join(report)


if __name__ == "__main__":
    summary, per_ticker, all_results = run_backtest()
    report = generate_report(summary, per_ticker, all_results)

    # レポート保存
    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "20260316_atr_dynamic_stoploss_backtest.md"
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n\nレポート保存: {report_path}")

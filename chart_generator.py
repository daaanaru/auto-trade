#!/usr/bin/env python3
"""
chart_generator.py — BUYシグナル解説チャート生成

BUYシグナルが出た銘柄のチャートを生成する。
ローソク足 + テクニカル指標（BB/RSI/EMA/MFI）+ シグナル矢印を描画。

使い方:
    python chart_generator.py                    # 現在BUYの銘柄を自動検出して生成
    python chart_generator.py --symbol AAPL       # 指定銘柄のチャート生成
    python chart_generator.py --symbol 6758.T --strategy monthly
    python chart_generator.py --all               # 全保有銘柄のチャート生成

出力先: docs/charts/
"""

import argparse
import json
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # GUI不要
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyArrowPatch
import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from plugins.indicators.feature_engine import FeatureEngine

CHART_DIR = os.path.join(BASE_DIR, "docs", "charts")
os.makedirs(CHART_DIR, exist_ok=True)

# ダークテーマ
plt.rcParams.update({
    "figure.facecolor": "#0d1117",
    "axes.facecolor": "#161b22",
    "axes.edgecolor": "#30363d",
    "axes.labelcolor": "#c9d1d9",
    "text.color": "#c9d1d9",
    "xtick.color": "#8b949e",
    "ytick.color": "#8b949e",
    "grid.color": "#21262d",
    "grid.alpha": 0.6,
    "font.size": 10,
})

# 日本語フォント（macOS）
for font in ["Hiragino Sans", "Hiragino Kaku Gothic Pro", "Arial Unicode MS"]:
    try:
        plt.rcParams["font.family"] = font
        break
    except Exception:
        continue


def fetch_data(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """yfinanceからデータ取得。"""
    df = yf.download(symbol, period=period, interval="1d", progress=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.dropna()
    return df


def load_params() -> dict:
    path = os.path.join(BASE_DIR, "optimized_params.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def draw_candlestick(ax, df, last_n=60):
    """ローソク足を描画。"""
    data = df.iloc[-last_n:].copy()
    x = np.arange(len(data))

    width = 0.6
    for i in range(len(data)):
        o, h, l, c = data["open"].iloc[i], data["high"].iloc[i], data["low"].iloc[i], data["close"].iloc[i]
        color = "#26a69a" if c >= o else "#ef5350"  # 緑=陽線, 赤=陰線
        # ヒゲ
        ax.plot([x[i], x[i]], [l, h], color=color, linewidth=0.8)
        # 実体
        body_bottom = min(o, c)
        body_height = abs(c - o)
        if body_height < 0.001:
            body_height = 0.001
        ax.bar(x[i], body_height, bottom=body_bottom, width=width, color=color, edgecolor=color, linewidth=0.5)

    return data, x


def generate_bb_rsi_chart(symbol: str, name: str, params: dict, period: str = "3mo"):
    """BB+RSI戦略のチャートを生成。"""
    df = fetch_data(symbol, period)
    if df is None or len(df) < 30:
        print(f"  {symbol}: データ不足、スキップ")
        return None

    fe = FeatureEngine()
    rsi_period = params.get("rsi_period", 14)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_oversold = params.get("rsi_oversold", 30)
    rsi_overbought = params.get("rsi_overbought", 70)

    df = fe.add_rsi(df, period=rsi_period)
    df = fe.add_bollinger_bands(df, period=bb_period, std_dev=bb_std)

    rsi_col = f"rsi_{rsi_period}"

    # シグナル計算
    buy_signals = (df["close"] <= df["bb_lower"]) & (df[rsi_col] < rsi_oversold)
    sell_signals = (df["close"] >= df["bb_upper"]) & (df[rsi_col] > rsi_overbought)

    last_n = min(60, len(df))
    data = df.iloc[-last_n:].copy()
    x = np.arange(len(data))

    fig = plt.figure(figsize=(14, 9))
    gs = gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1], hspace=0.08)

    # === メインチャート（ローソク足 + BB） ===
    ax1 = fig.add_subplot(gs[0])
    _, _ = draw_candlestick(ax1, df, last_n)

    # ボリンジャーバンド
    bb_upper_data = data["bb_upper"].values
    bb_lower_data = data["bb_lower"].values
    bb_mid_data = data["bb_mid"].values if "bb_mid" in data.columns else (bb_upper_data + bb_lower_data) / 2

    ax1.plot(x, bb_upper_data, color="#f59e0b", linewidth=1, alpha=0.8, label=f"BB上限({bb_period}日±{bb_std}σ)")
    ax1.plot(x, bb_lower_data, color="#f59e0b", linewidth=1, alpha=0.8, label=f"BB下限")
    ax1.plot(x, bb_mid_data, color="#f59e0b", linewidth=0.7, alpha=0.4, linestyle="--")
    ax1.fill_between(x, bb_lower_data, bb_upper_data, alpha=0.05, color="#f59e0b")

    # BUY/SELLマーカー
    buy_data = buy_signals.iloc[-last_n:]
    sell_data = sell_signals.iloc[-last_n:]
    for i in range(len(data)):
        if buy_data.iloc[i]:
            ax1.annotate("BUY", xy=(x[i], data["low"].iloc[i]),
                         xytext=(x[i], data["low"].iloc[i] - (data["high"].max() - data["low"].min()) * 0.08),
                         color="#2ecc71", fontsize=8, fontweight="bold", ha="center",
                         arrowprops=dict(arrowstyle="->", color="#2ecc71", lw=1.5))
        if sell_data.iloc[i]:
            ax1.annotate("SELL", xy=(x[i], data["high"].iloc[i]),
                         xytext=(x[i], data["high"].iloc[i] + (data["high"].max() - data["low"].min()) * 0.08),
                         color="#ef5350", fontsize=8, fontweight="bold", ha="center",
                         arrowprops=dict(arrowstyle="->", color="#ef5350", lw=1.5))

    ax1.set_title(f"{name} ({symbol}) — BB+RSI逆張り戦略", fontsize=14, fontweight="bold", pad=12)
    ax1.legend(loc="upper left", fontsize=8, framealpha=0.3)
    ax1.set_ylabel("価格")
    ax1.grid(True, linewidth=0.3)
    ax1.set_xlim(-1, len(data))

    # x軸ラベル設定（日付を間引いて表示）
    date_labels = [d.strftime("%m/%d") for d in data.index]
    tick_positions = list(range(0, len(data), max(1, len(data) // 10)))
    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels([date_labels[i] for i in tick_positions], fontsize=7)

    # === RSI ===
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    rsi_data = data[rsi_col].values
    ax2.plot(x, rsi_data, color="#58a6ff", linewidth=1.2, label=f"RSI({rsi_period})")
    ax2.axhline(rsi_oversold, color="#2ecc71", linewidth=0.8, linestyle="--", alpha=0.7, label=f"売られすぎ({rsi_oversold})")
    ax2.axhline(rsi_overbought, color="#ef5350", linewidth=0.8, linestyle="--", alpha=0.7, label=f"買われすぎ({rsi_overbought})")
    ax2.axhline(50, color="#8b949e", linewidth=0.5, linestyle=":", alpha=0.5)
    ax2.fill_between(x, rsi_data, rsi_oversold, where=(rsi_data < rsi_oversold), alpha=0.15, color="#2ecc71")
    ax2.fill_between(x, rsi_data, rsi_overbought, where=(rsi_data > rsi_overbought), alpha=0.15, color="#ef5350")
    ax2.set_ylabel("RSI")
    ax2.set_ylim(0, 100)
    ax2.legend(loc="upper left", fontsize=7, framealpha=0.3)
    ax2.grid(True, linewidth=0.3)

    # === 出来高 ===
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    colors = ["#26a69a" if data["close"].iloc[i] >= data["open"].iloc[i] else "#ef5350" for i in range(len(data))]
    ax3.bar(x, data["volume"].values, color=colors, alpha=0.6, width=0.7)
    ax3.set_ylabel("出来高")
    ax3.grid(True, linewidth=0.3)
    ax3.set_xlim(-1, len(data))

    # 現在の状態をテキストで表示
    current_rsi = rsi_data[-1] if not np.isnan(rsi_data[-1]) else 0
    current_price = data["close"].iloc[-1]
    current_bb_lower = bb_lower_data[-1]
    current_bb_upper = bb_upper_data[-1]

    status_text = f"現在値: {current_price:,.1f}  RSI: {current_rsi:.0f}  BB下限: {current_bb_lower:,.1f}  BB上限: {current_bb_upper:,.1f}"
    fig.text(0.5, 0.01, status_text, ha="center", fontsize=9, color="#8b949e")

    # 判定結果
    is_buy = current_price <= current_bb_lower and current_rsi < rsi_oversold
    signal_text = "現在のシグナル: BUY" if is_buy else "現在のシグナル: NEUTRAL"
    signal_color = "#2ecc71" if is_buy else "#8b949e"
    fig.text(0.5, 0.035, signal_text, ha="center", fontsize=11, fontweight="bold", color=signal_color)

    plt.tight_layout(rect=[0, 0.05, 1, 1])

    safe_symbol = symbol.replace("=", "_").replace("/", "_")
    path = os.path.join(CHART_DIR, f"{safe_symbol}_bb_rsi.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  保存: {path}")
    return path


def generate_monthly_momentum_chart(symbol: str, name: str, params: dict, period: str = "6mo"):
    """月初モメンタム戦略のチャートを生成。"""
    df = fetch_data(symbol, period)
    if df is None or len(df) < 30:
        print(f"  {symbol}: データ不足、スキップ")
        return None

    entry_days = params.get("entry_days", 7)
    vol_ma_period = params.get("volume_ma_period", 20)
    vol_threshold = params.get("volume_threshold", 2.0)

    vol_ma = df["volume"].rolling(window=vol_ma_period).mean()
    vol_ratio = df["volume"] / vol_ma

    # シグナル計算
    buy_signals = pd.Series(False, index=df.index)
    sell_signals = pd.Series(False, index=df.index)
    for i in range(len(df)):
        day = df.index[i].day
        if day <= entry_days and vol_ratio.iloc[i] >= vol_threshold:
            buy_signals.iloc[i] = True
        if i < len(df) - 1 and df.index[i + 1].month != df.index[i].month:
            sell_signals.iloc[i] = True

    last_n = min(90, len(df))
    data = df.iloc[-last_n:].copy()
    x = np.arange(len(data))
    vol_ratio_data = vol_ratio.iloc[-last_n:].values

    fig = plt.figure(figsize=(14, 9))
    gs = gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1], hspace=0.08)

    # === メインチャート ===
    ax1 = fig.add_subplot(gs[0])
    draw_candlestick(ax1, df, last_n)

    # 月初ゾーンをハイライト
    prev_month = None
    for i in range(len(data)):
        dt = data.index[i]
        if dt.month != prev_month:
            # 月の切り替わり
            end_i = min(i + entry_days, len(data))
            ax1.axvspan(i - 0.5, end_i - 0.5, alpha=0.06, color="#58a6ff")
        prev_month = dt.month

    # BUY/SELLマーカー
    buy_data = buy_signals.iloc[-last_n:]
    sell_data = sell_signals.iloc[-last_n:]
    price_range = data["high"].max() - data["low"].min()
    for i in range(len(data)):
        if buy_data.iloc[i]:
            ax1.annotate("BUY", xy=(x[i], data["low"].iloc[i]),
                         xytext=(x[i], data["low"].iloc[i] - price_range * 0.08),
                         color="#2ecc71", fontsize=8, fontweight="bold", ha="center",
                         arrowprops=dict(arrowstyle="->", color="#2ecc71", lw=1.5))
        if sell_data.iloc[i]:
            ax1.annotate("SELL", xy=(x[i], data["high"].iloc[i]),
                         xytext=(x[i], data["high"].iloc[i] + price_range * 0.08),
                         color="#ef5350", fontsize=8, fontweight="bold", ha="center",
                         arrowprops=dict(arrowstyle="->", color="#ef5350", lw=1.5))

    ax1.set_title(f"{name} ({symbol}) — 月初モメンタム戦略", fontsize=14, fontweight="bold", pad=12)
    ax1.set_ylabel("価格")
    ax1.grid(True, linewidth=0.3)
    ax1.set_xlim(-1, len(data))

    date_labels = [d.strftime("%m/%d") for d in data.index]
    tick_positions = list(range(0, len(data), max(1, len(data) // 10)))
    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels([date_labels[i] for i in tick_positions], fontsize=7)

    # === 出来高比率 ===
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    colors = ["#2ecc71" if v >= vol_threshold else "#8b949e" for v in vol_ratio_data]
    ax2.bar(x, vol_ratio_data, color=colors, alpha=0.7, width=0.7)
    ax2.axhline(vol_threshold, color="#f59e0b", linewidth=1, linestyle="--", label=f"閾値({vol_threshold}倍)")
    ax2.set_ylabel(f"出来高/{vol_ma_period}日平均")
    ax2.legend(loc="upper left", fontsize=7, framealpha=0.3)
    ax2.grid(True, linewidth=0.3)

    # === 出来高 ===
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    vol_colors = ["#26a69a" if data["close"].iloc[i] >= data["open"].iloc[i] else "#ef5350" for i in range(len(data))]
    ax3.bar(x, data["volume"].values, color=vol_colors, alpha=0.6, width=0.7)
    ax3.set_ylabel("出来高")
    ax3.grid(True, linewidth=0.3)

    # ステータス
    current_day = data.index[-1].day
    current_ratio = vol_ratio_data[-1] if not np.isnan(vol_ratio_data[-1]) else 0
    is_buy = current_day <= entry_days and current_ratio >= vol_threshold
    status_text = f"月{current_day}日目（エントリー: {entry_days}日以内）  出来高比率: {current_ratio:.1f}倍（閾値: {vol_threshold}倍）"
    signal_text = "現在のシグナル: BUY" if is_buy else "現在のシグナル: NEUTRAL"
    signal_color = "#2ecc71" if is_buy else "#8b949e"

    fig.text(0.5, 0.01, status_text, ha="center", fontsize=9, color="#8b949e")
    fig.text(0.5, 0.035, signal_text, ha="center", fontsize=11, fontweight="bold", color=signal_color)

    plt.tight_layout(rect=[0, 0.05, 1, 1])

    safe_symbol = symbol.replace("=", "_").replace("/", "_")
    path = os.path.join(CHART_DIR, f"{safe_symbol}_monthly.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  保存: {path}")
    return path


def generate_volume_divergence_chart(symbol: str, name: str, params: dict, period: str = "1y"):
    """出来高ダイバージェンス戦略のチャートを生成。"""
    df = fetch_data(symbol, period)
    if df is None or len(df) < 100:
        print(f"  {symbol}: データ不足、スキップ")
        return None

    fe = FeatureEngine()
    mfi_period = params.get("mfi_period", 14)
    ema_period = params.get("ema_period", 200)
    vo_short = params.get("vo_short", 5)
    vo_long = params.get("vo_long", 10)

    df = fe.add_mfi(df, period=mfi_period)
    df = fe.add_ema(df, period=ema_period)
    df = fe.add_volume_oscillator(df, short=vo_short, long=vo_long)

    mfi_col = f"mfi_{mfi_period}"
    ema_col = f"ema_{ema_period}"

    last_n = min(90, len(df))
    data = df.iloc[-last_n:].copy()
    x = np.arange(len(data))

    fig = plt.figure(figsize=(14, 10))
    gs = gridspec.GridSpec(4, 1, height_ratios=[3, 1, 1, 0.8], hspace=0.08)

    # === メインチャート + EMA ===
    ax1 = fig.add_subplot(gs[0])
    draw_candlestick(ax1, df, last_n)

    if ema_col in data.columns:
        ema_data = data[ema_col].values
        ax1.plot(x, ema_data, color="#ef5350", linewidth=1.5, alpha=0.8, label=f"{ema_period}日EMA（トレンドフィルター）")

    ax1.set_title(f"{name} ({symbol}) — 出来高ダイバージェンス戦略", fontsize=14, fontweight="bold", pad=12)
    ax1.legend(loc="upper left", fontsize=8, framealpha=0.3)
    ax1.set_ylabel("価格")
    ax1.grid(True, linewidth=0.3)
    ax1.set_xlim(-1, len(data))

    date_labels = [d.strftime("%m/%d") for d in data.index]
    tick_positions = list(range(0, len(data), max(1, len(data) // 10)))
    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels([date_labels[i] for i in tick_positions], fontsize=7)

    # === MFI ===
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    if mfi_col in data.columns:
        mfi_data = data[mfi_col].values
        ax2.plot(x, mfi_data, color="#d2a8ff", linewidth=1.2, label=f"MFI({mfi_period})")
        ax2.axhline(30, color="#2ecc71", linewidth=0.8, linestyle="--", alpha=0.5)
        ax2.axhline(70, color="#ef5350", linewidth=0.8, linestyle="--", alpha=0.5)
        ax2.fill_between(x, mfi_data, 30, where=(mfi_data < 30), alpha=0.15, color="#2ecc71")
    ax2.set_ylabel("MFI")
    ax2.set_ylim(0, 100)
    ax2.legend(loc="upper left", fontsize=7, framealpha=0.3)
    ax2.grid(True, linewidth=0.3)

    # === Volume Oscillator ===
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    if "vol_osc" in data.columns:
        vo_data = data["vol_osc"].values
        colors = ["#2ecc71" if v > 0 else "#ef5350" for v in vo_data]
        ax3.bar(x, vo_data, color=colors, alpha=0.6, width=0.7)
        ax3.axhline(0, color="#8b949e", linewidth=0.5)
    ax3.set_ylabel(f"VO({vo_short}/{vo_long})")
    ax3.legend(loc="upper left", fontsize=7, framealpha=0.3)
    ax3.grid(True, linewidth=0.3)

    # === 出来高 ===
    ax4 = fig.add_subplot(gs[3], sharex=ax1)
    vol_colors = ["#26a69a" if data["close"].iloc[i] >= data["open"].iloc[i] else "#ef5350" for i in range(len(data))]
    ax4.bar(x, data["volume"].values, color=vol_colors, alpha=0.6, width=0.7)
    ax4.set_ylabel("出来高")
    ax4.grid(True, linewidth=0.3)

    # ステータス
    current_mfi = float(data[mfi_col].iloc[-1]) if mfi_col in data.columns and not pd.isna(data[mfi_col].iloc[-1]) else 0
    current_price = float(data["close"].iloc[-1])
    current_ema = float(data[ema_col].iloc[-1]) if ema_col in data.columns and not pd.isna(data[ema_col].iloc[-1]) else 0
    trend = "上昇" if current_price > current_ema else "下降"

    status_text = f"MFI: {current_mfi:.0f}  価格: {current_price:,.0f}  {ema_period}EMA: {current_ema:,.0f}  トレンド: {trend}"
    fig.text(0.5, 0.01, status_text, ha="center", fontsize=9, color="#8b949e")

    plt.tight_layout(rect=[0, 0.03, 1, 1])

    safe_symbol = symbol.replace("=", "_").replace("/", "_").replace("-", "_")
    path = os.path.join(CHART_DIR, f"{safe_symbol}_vol_div.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  保存: {path}")
    return path


def generate_for_portfolio():
    """現在保有中の銘柄のチャートを全生成。"""
    portfolio_path = os.path.join(BASE_DIR, "paper_portfolio.json")
    if not os.path.exists(portfolio_path):
        print("paper_portfolio.json が見つかりません")
        return

    with open(portfolio_path) as f:
        portfolio = json.load(f)

    params = load_params()
    generated = []

    for pos in portfolio.get("positions", []):
        symbol = pos["code"]
        name = pos.get("name", symbol)
        strategy = pos.get("strategy", "bb_rsi")

        print(f"\n  生成中: {name} ({symbol}) [{strategy}]")

        if strategy == "bb_rsi":
            path = generate_bb_rsi_chart(symbol, name, params.get("bb_rsi", {}))
        elif strategy == "monthly":
            path = generate_monthly_momentum_chart(symbol, name, params.get("monthly", {}))
        elif strategy == "vol_div":
            path = generate_volume_divergence_chart(symbol, name, params.get("vol_div", {}))
        else:
            path = generate_bb_rsi_chart(symbol, name, params.get("bb_rsi", {}))

        if path:
            generated.append(path)

    return generated


def main():
    parser = argparse.ArgumentParser(description="BUYシグナル解説チャート生成")
    parser.add_argument("--symbol", help="銘柄コード（例: AAPL, 6758.T, BTC-JPY）")
    parser.add_argument("--name", help="銘柄名（省略時はシンボルを使用）")
    parser.add_argument("--strategy", choices=["bb_rsi", "monthly", "vol_div"],
                        default="bb_rsi", help="戦略（デフォルト: bb_rsi）")
    parser.add_argument("--all", action="store_true", help="全保有銘柄のチャートを生成")
    parser.add_argument("--period", default=None, help="データ期間（例: 3mo, 6mo, 1y）")
    args = parser.parse_args()

    params = load_params()

    if args.all:
        generate_for_portfolio()
        return

    if not args.symbol:
        # デフォルト: 保有銘柄を全生成
        print("保有銘柄のチャートを生成します...")
        generate_for_portfolio()
        return

    symbol = args.symbol
    name = args.name or symbol

    if args.strategy == "bb_rsi":
        period = args.period or "3mo"
        generate_bb_rsi_chart(symbol, name, params.get("bb_rsi", {}), period)
    elif args.strategy == "monthly":
        period = args.period or "6mo"
        generate_monthly_momentum_chart(symbol, name, params.get("monthly", {}), period)
    elif args.strategy == "vol_div":
        period = args.period or "1y"
        generate_volume_divergence_chart(symbol, name, params.get("vol_div", {}), period)


if __name__ == "__main__":
    main()

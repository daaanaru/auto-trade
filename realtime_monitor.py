#!/usr/bin/env python3
"""
realtime_monitor.py — リアルタイム・テクニカル指標モニター

15分間隔で監視銘柄のRSI・ATRをポーリングし、
閾値を超えたらDiscord即時通知する。

使い方:
    python3 realtime_monitor.py                # 1回実行
    python3 realtime_monitor.py --loop          # 15分間隔で常駐
    python3 realtime_monitor.py --ticker BTC-USD  # 単一銘柄

設計:
    - yfinance interval="15m" で直近2日分の15分足を取得
    - FeatureEngine でRSI/ATR/BB/MACDを計算
    - 閾値超えをDiscord通知（重複通知は1時間抑制）
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from plugins.indicators.feature_engine import FeatureEngine
from notifier import send_discord_embed

# --- 設定 ---

# 監視対象（デフォルト）
DEFAULT_WATCHLIST = {
    "btc": ["BTC-USD"],
    "gold": ["GLD", "GC=F"],
    "us": ["AAPL", "NVDA", "GOOGL", "MSFT", "AMZN", "META", "TSLA", "JPM"],
    "jp": ["6758.T", "9984.T", "7203.T", "8306.T"],
    "fx": ["USDJPY=X", "EURUSD=X"],
}

# アラート閾値
THRESHOLDS = {
    "rsi_oversold": 30,       # RSI <= 30 で売られすぎ
    "rsi_overbought": 70,     # RSI >= 70 で買われすぎ
    "atr_spike_ratio": 2.0,   # ATR が20日平均の2倍以上でボラ急騰
    "bb_squeeze_width": 0.03, # BBバンド幅 <= 3% でスクイーズ（爆発前兆）
    "bb_breakout_upper": True, # BB上限ブレイクアウト
    "bb_breakout_lower": True, # BB下限ブレイクアウト
}

POLL_INTERVAL_SEC = 15 * 60  # 15分
COOLDOWN_SEC = 60 * 60       # 同一アラートの再通知抑制（1時間）

# 通知履歴ファイル（重複通知防止）
ALERT_HISTORY_PATH = os.path.join(BASE_DIR, "realtime_alert_history.json")


def load_alert_history():
    if os.path.exists(ALERT_HISTORY_PATH):
        try:
            with open(ALERT_HISTORY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_alert_history(history):
    with open(ALERT_HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def is_cooled_down(history, key):
    """同一アラートがクールダウン期間内かチェック"""
    last = history.get(key)
    if last is None:
        return True
    last_time = datetime.fromisoformat(last)
    return (datetime.now() - last_time).total_seconds() > COOLDOWN_SEC


def fetch_intraday(ticker, period="2d", interval="15m"):
    """yfinanceで15分足を取得"""
    try:
        data = yf.download(ticker, period=period, interval=interval, progress=False)
        if data is None or len(data) < 10:
            return None
        # マルチレベルカラムの場合にフラット化
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [c[0].lower() for c in data.columns]
        else:
            data.columns = [c.lower() for c in data.columns]
        # volumeが無い場合のフォールバック（FXなど）
        if "volume" not in data.columns:
            data["volume"] = 0
        return data
    except Exception as e:
        print(f"  [ERROR] {ticker} データ取得失敗: {e}")
        return None


def analyze_ticker(ticker, fe):
    """1銘柄のテクニカル分析を実行してアラート条件をチェック"""
    df = fetch_intraday(ticker)
    if df is None:
        return []

    # テクニカル指標計算
    df = fe.add_rsi(df)
    df = fe.add_atr(df)
    df = fe.add_bollinger_bands(df)

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest

    alerts = []
    price = latest["close"]
    rsi = latest.get("rsi_14")
    atr = latest.get("atr_14")
    bb_upper = latest.get("bb_upper")
    bb_lower = latest.get("bb_lower")
    bb_wband = latest.get("bb_wband")

    # ATR 20期間平均
    atr_col = "atr_14"
    if atr_col in df.columns:
        atr_avg = df[atr_col].rolling(20).mean().iloc[-1]
    else:
        atr_avg = None

    # --- RSI アラート ---
    if rsi is not None and not pd.isna(rsi):
        if rsi <= THRESHOLDS["rsi_oversold"]:
            alerts.append({
                "type": "RSI_OVERSOLD",
                "ticker": ticker,
                "value": f"RSI={rsi:.1f}",
                "price": price,
                "message": f"RSI {rsi:.1f} <= {THRESHOLDS['rsi_oversold']}（売られすぎ）",
                "color": 0x00FF00,  # 買いチャンス = 緑
            })
        elif rsi >= THRESHOLDS["rsi_overbought"]:
            alerts.append({
                "type": "RSI_OVERBOUGHT",
                "ticker": ticker,
                "value": f"RSI={rsi:.1f}",
                "price": price,
                "message": f"RSI {rsi:.1f} >= {THRESHOLDS['rsi_overbought']}（買われすぎ）",
                "color": 0xFF0000,  # 売りシグナル = 赤
            })

    # --- ATR スパイク ---
    if atr is not None and atr_avg is not None and not pd.isna(atr) and not pd.isna(atr_avg) and atr_avg > 0:
        ratio = atr / atr_avg
        if ratio >= THRESHOLDS["atr_spike_ratio"]:
            alerts.append({
                "type": "ATR_SPIKE",
                "ticker": ticker,
                "value": f"ATR={atr:.4f} ({ratio:.1f}x平均)",
                "price": price,
                "message": f"ATR急騰 {ratio:.1f}倍（ボラティリティ異常）",
                "color": 0xFFA500,  # オレンジ
            })

    # --- BB スクイーズ（バンド幅縮小 = 爆発前兆）---
    if bb_wband is not None and not pd.isna(bb_wband):
        if bb_wband <= THRESHOLDS["bb_squeeze_width"]:
            alerts.append({
                "type": "BB_SQUEEZE",
                "ticker": ticker,
                "value": f"BandWidth={bb_wband:.4f}",
                "price": price,
                "message": f"BBスクイーズ（バンド幅 {bb_wband:.4f} <= {THRESHOLDS['bb_squeeze_width']}）— 大きな値動きの前兆",
                "color": 0x9B59B6,  # 紫
            })

    # --- BB ブレイクアウト ---
    if bb_upper is not None and not pd.isna(bb_upper) and price > bb_upper:
        alerts.append({
            "type": "BB_BREAK_UPPER",
            "ticker": ticker,
            "value": f"Price={price:.2f} > BB上限={bb_upper:.2f}",
            "price": price,
            "message": f"BB上限ブレイクアウト（強い上昇圧力）",
            "color": 0x2ECC71,
        })
    if bb_lower is not None and not pd.isna(bb_lower) and price < bb_lower:
        alerts.append({
            "type": "BB_BREAK_LOWER",
            "ticker": ticker,
            "value": f"Price={price:.2f} < BB下限={bb_lower:.2f}",
            "price": price,
            "message": f"BB下限ブレイクアウト（反発の可能性）",
            "color": 0xE74C3C,
        })

    return alerts


def send_alerts(alerts, history):
    """アラートをDiscord通知（クールダウン考慮）"""
    sent = 0
    for alert in alerts:
        key = f"{alert['ticker']}_{alert['type']}"
        if not is_cooled_down(history, key):
            continue

        fields = [
            {"name": "銘柄", "value": alert["ticker"], "inline": True},
            {"name": "価格", "value": f"{alert['price']:,.2f}", "inline": True},
            {"name": "検出値", "value": alert["value"], "inline": True},
        ]
        send_discord_embed(
            title=f"[RT] {alert['type']} — {alert['ticker']}",
            description=alert["message"],
            color=alert["color"],
            fields=fields,
            username="realtime-monitor",
        )
        history[key] = datetime.now().isoformat()
        sent += 1

    return sent


def run_scan(watchlist=None, verbose=True):
    """全監視銘柄をスキャン"""
    if watchlist is None:
        watchlist = DEFAULT_WATCHLIST

    fe = FeatureEngine()
    history = load_alert_history()
    all_alerts = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if verbose:
        print(f"\n{'='*60}")
        print(f"  リアルタイムモニター  {now}")
        print(f"{'='*60}")

    for market, tickers in watchlist.items():
        if verbose:
            print(f"\n  [{market.upper()}] {len(tickers)}銘柄スキャン中...")

        for ticker in tickers:
            alerts = analyze_ticker(ticker, fe)
            if alerts:
                all_alerts.extend(alerts)
                if verbose:
                    for a in alerts:
                        print(f"    ** {a['ticker']}: {a['message']}")

    # Discord通知
    sent = send_alerts(all_alerts, history)
    save_alert_history(history)

    if verbose:
        print(f"\n  アラート: {len(all_alerts)}件検出, {sent}件通知")
        if not all_alerts:
            print("  異常なし")
        print()

    return all_alerts


def main():
    parser = argparse.ArgumentParser(description="リアルタイム・テクニカル指標モニター")
    parser.add_argument("--loop", action="store_true", help="15分間隔で常駐実行")
    parser.add_argument("--ticker", type=str, help="単一銘柄を指定してスキャン")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC,
                        help=f"ポーリング間隔（秒、デフォルト{POLL_INTERVAL_SEC}）")
    args = parser.parse_args()

    if args.ticker:
        watchlist = {"custom": [args.ticker]}
    else:
        watchlist = DEFAULT_WATCHLIST

    if args.loop:
        print(f"常駐モード開始（{args.interval}秒間隔）")
        while True:
            try:
                run_scan(watchlist)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n終了")
                break
            except Exception as e:
                print(f"  [ERROR] スキャンエラー: {e}")
                time.sleep(60)
    else:
        run_scan(watchlist)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
全市場統合スクリーナー

日本株・米国株・BTC・ゴールド・FXを1つのスクリプトで一括スキャンし、
市場ごとに最適な戦略でシグナルを判定する。

対応市場と戦略:
  - 日本株（日経225 watchlist）: Monthly Momentum
  - 米国株（主要50銘柄）      : BB+RSI Combo
  - BTC（BTC-USD）            : Volume Divergence
  - ゴールド（GLD ETF）       : BB+RSI Combo
  - FX（外国為替）            : BB+RSI Combo

使い方:
    python unified_screener.py                       # 全市場スキャン
    python unified_screener.py --market jp            # 日本株のみ
    python unified_screener.py --market us            # 米国株のみ
    python unified_screener.py --market btc           # BTCのみ
    python unified_screener.py --market gold          # ゴールドのみ
    python unified_screener.py --market fx            # FXのみ
    python unified_screener.py --json                 # JSON出力
    python unified_screener.py --buy-only             # BUYシグナルのみ
    python unified_screener.py --full                 # フルスクリーニング（WF検証付き）

cron設定例（毎朝6:00に全市場スキャン）:
    0 6 * * * cd /path/to/auto-trade && python3 unified_screener.py --json >> logs/unified-screener.log 2>&1
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

# auto-trade ディレクトリをパスに追加
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from market_hours import should_scan
from engine import BacktestEngine, BacktestConfig
from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy


# ==============================================================
# 市場定義
# ==============================================================

MARKET_CONFIG = {
    "jp": {
        "name": "日本株",
        "strategy_key": "monthly",
        "strategy_class": MonthlyMomentumStrategy,
        "tickers_file": "watchlist.json",
        "tickers_loader": "watchlist",  # watchlist形式
        "data_period": "3mo",
        "fetch_delay": 0.5,
    },
    "us": {
        "name": "米国株",
        "strategy_key": "bb_rsi",
        "strategy_class": BBRSIComboStrategy,
        "tickers_file": "us_stock_tickers.json",
        "tickers_loader": "ticker_list",  # {"tickers": [...]} 形式
        "data_period": "3mo",
        "fetch_delay": 0.3,
    },
    "btc": {
        "name": "仮想通貨",
        "strategy_key": "vol_div",
        "strategy_class": VolumeDivergenceStrategy,
        "tickers_file": None,  # 固定銘柄
        "tickers_loader": "fixed",
        "fixed_tickers": [
            {"code": "BTC-JPY",  "name": "ビットコイン"},
            {"code": "ETH-JPY",  "name": "イーサリアム"},
            {"code": "XRP-JPY",  "name": "リップル"},
            {"code": "XLM-JPY",  "name": "ステラルーメン"},
            {"code": "MONA-JPY", "name": "モナコイン"},
        ],
        "data_period": "1y",  # Volume Divergenceは長期データが必要
        "fetch_delay": 0.3,
    },
    "gold": {
        "name": "ゴールド",
        "strategy_key": "bb_rsi",
        "strategy_class": BBRSIComboStrategy,
        "tickers_file": None,
        "tickers_loader": "fixed",
        "fixed_tickers": [
            {"code": "GLD", "name": "SPDR Gold Shares ETF"},
            {"code": "GC=F", "name": "Gold Futures"},
        ],
        "data_period": "3mo",
        "fetch_delay": 0.3,
    },
    "fx": {
        "name": "外国為替",
        "strategy_key": "bb_rsi",
        "strategy_class": BBRSIComboStrategy,
        "tickers_file": "fx_tickers.json",
        "tickers_loader": "ticker_list",
        "data_period": "3mo",
        "fetch_delay": 0.3,
    },
}


# ==============================================================
# データ取得
# ==============================================================

def fetch_data(symbol: str, period: str = "3mo", interval: str = "1d",
               max_retries: int = 3) -> Optional[pd.DataFrame]:
    """yfinance からデータを取得する。リトライ付き。"""
    min_rows = 5 if interval != "1d" else 30
    for attempt in range(max_retries):
        try:
            df = yf.download(symbol, period=period, interval=interval, progress=False)
            if df.empty:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
            df = df.dropna()
            if len(df) < min_rows:
                return None
            return df
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"  [ERROR] {symbol}: {e} (リトライ{max_retries}回失敗)")
            return None
    return None


# ==============================================================
# ティッカー読み込み
# ==============================================================

def load_tickers(market_key: str) -> list:
    """市場設定に応じてティッカーリストを返す。

    Returns:
        [{"code": "6758.T", "name": "ソニー"}, ...]
    """
    config = MARKET_CONFIG[market_key]
    loader = config["tickers_loader"]

    if loader == "fixed":
        return config["fixed_tickers"]

    tickers_path = os.path.join(BASE_DIR, config["tickers_file"])
    if not os.path.exists(tickers_path):
        print(f"  WARNING: {config['tickers_file']} が見つかりません。スキップします。")
        return []

    with open(tickers_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if loader == "watchlist":
        return data.get("symbols", [])
    elif loader == "ticker_list":
        return data.get("tickers", [])

    return []


def load_params() -> dict:
    """optimized_params.json から全戦略のパラメータを読み込む。"""
    params_path = os.path.join(BASE_DIR, "optimized_params.json")
    if not os.path.exists(params_path):
        return {}
    with open(params_path, "r") as f:
        return json.load(f)


# ==============================================================
# シグナル判定
# ==============================================================

def analyze_ticker(code: str, name: str, strategy, period: str) -> dict:
    """1銘柄を分析してシグナル情報を返す。"""
    data = fetch_data(code, period=period)
    if data is None:
        return {
            "code": code,
            "name": name,
            "signal": "ERROR",
            "price": 0.0,
            "change_pct": 0.0,
            "score": 0.0,
            "data_date": "",
            "error": "データ取得失敗",
        }

    try:
        signals = strategy.generate_signals(data)
        today_signal = int(signals.iloc[-1])

        # シグナルラベル
        if today_signal == 1:
            signal_label = "BUY"
        elif today_signal == -1:
            signal_label = "SELL"
        else:
            signal_label = "NEUTRAL"

        # 価格情報
        current_price = float(data["close"].iloc[-1])
        prev_price = float(data["close"].iloc[-2]) if len(data) > 1 else current_price
        change_pct = ((current_price - prev_price) / prev_price) * 100

        # スコア（戦略に応じて計算）
        score = _calc_score(data, strategy, signals)

        # シグナル理由（BUY/SELLのみ）
        reason = _build_reason(data, strategy, signal_label)

        return {
            "code": code,
            "name": name,
            "signal": signal_label,
            "price": round(current_price, 2),
            "change_pct": round(change_pct, 2),
            "score": round(score, 2),
            "reason": reason,
            "data_date": data.index[-1].strftime("%Y-%m-%d"),
            "error": None,
        }
    except Exception as e:
        return {
            "code": code,
            "name": name,
            "signal": "ERROR",
            "price": 0.0,
            "change_pct": 0.0,
            "score": 0.0,
            "data_date": "",
            "error": str(e),
        }


def _build_reason(data: pd.DataFrame, strategy, signal_label: str) -> str:
    """BUY/SELLシグナルの理由を人間が読める日本語テキストで返す。"""
    if signal_label == "NEUTRAL" or signal_label == "ERROR":
        return ""

    strategy_name = strategy.meta.name
    reasons = []

    if strategy_name == "Monthly_Momentum":
        day = data.index[-1].day
        entry_days = strategy.params.get("entry_days", 7)
        vol_ma_period = strategy.params.get("volume_ma_period", 20)
        vol_threshold = strategy.params.get("volume_threshold", 2.0)
        vol_ma = data["volume"].rolling(window=vol_ma_period).mean()
        vol_ratio = float(data["volume"].iloc[-1] / vol_ma.iloc[-1]) if vol_ma.iloc[-1] > 0 else 0

        if signal_label == "BUY":
            reasons.append(f"月初{day}日目（{entry_days}日以内）")
            reasons.append(f"出来高が{vol_ma_period}日平均の{vol_ratio:.1f}倍（閾値{vol_threshold}倍）")
        elif signal_label == "SELL":
            reasons.append("月末手仕舞いルール")

    elif strategy_name == "BB_RSI_Combo":
        from plugins.indicators.feature_engine import FeatureEngine
        fe = FeatureEngine()
        rsi_period = strategy.params.get("rsi_period", 14)
        bb_period = strategy.params.get("bb_period", 20)
        bb_std = strategy.params.get("bb_std", 2.0)
        rsi_oversold = strategy.params.get("rsi_oversold", 30)
        rsi_overbought = strategy.params.get("rsi_overbought", 70)

        df = fe.add_rsi(data.copy(), period=rsi_period)
        df = fe.add_bollinger_bands(df, period=bb_period, std_dev=bb_std)
        rsi_val = float(df[f"rsi_{rsi_period}"].iloc[-1]) if f"rsi_{rsi_period}" in df.columns else 50
        close = float(df["close"].iloc[-1])
        bb_lower = float(df["bb_lower"].iloc[-1]) if "bb_lower" in df.columns else 0
        bb_upper = float(df["bb_upper"].iloc[-1]) if "bb_upper" in df.columns else 0

        if signal_label == "BUY":
            reasons.append(f"RSI={rsi_val:.0f}（{rsi_oversold}以下で売られすぎ）")
            reasons.append(f"価格{close:,.1f} ≤ BB下限{bb_lower:,.1f}（{bb_period}日±{bb_std}σ）")
        elif signal_label == "SELL":
            reasons.append(f"RSI={rsi_val:.0f}（{rsi_overbought}以上で買われすぎ）")
            reasons.append(f"価格{close:,.1f} ≥ BB上限{bb_upper:,.1f}")

    elif strategy_name == "Volume_Divergence":
        from plugins.indicators.feature_engine import FeatureEngine
        fe = FeatureEngine()
        mfi_period = strategy.params.get("mfi_period", 14)
        ema_period = strategy.params.get("ema_period", 200)

        df = fe.add_mfi(data.copy(), period=mfi_period)
        df = fe.add_ema(df, period=ema_period)
        df = fe.add_volume_oscillator(df, short=strategy.params.get("vo_short", 5), long=strategy.params.get("vo_long", 10))

        mfi_val = float(df[f"mfi_{mfi_period}"].iloc[-1]) if f"mfi_{mfi_period}" in df.columns else 50
        ema_val = float(df[f"ema_{ema_period}"].iloc[-1]) if f"ema_{ema_period}" in df.columns else 0
        close = float(df["close"].iloc[-1])

        if signal_label == "BUY":
            reasons.append(f"MFI強気ダイバージェンス検出（MFI={mfi_val:.0f}）")
            reasons.append(f"価格{close:,.0f} > {ema_period}日EMA{ema_val:,.0f}（上昇トレンド）")
            reasons.append("出来高オシレーター上昇中")
        elif signal_label == "SELL":
            reasons.append(f"MFI弱気ダイバージェンス検出（MFI={mfi_val:.0f}）")
            reasons.append(f"価格{close:,.0f} < {ema_period}日EMA{ema_val:,.0f}（下降トレンド）")

    return " / ".join(reasons) if reasons else ""


def _calc_score(data: pd.DataFrame, strategy, signals: pd.Series) -> float:
    """戦略に応じたスコアを計算する。"""
    strategy_name = strategy.meta.name

    if strategy_name == "Monthly_Momentum":
        # 出来高比率をスコアとして使う
        vol_ma_period = strategy.params.get("volume_ma_period", 20)
        vol_ma = data["volume"].rolling(window=vol_ma_period).mean()
        if vol_ma.iloc[-1] == 0 or pd.isna(vol_ma.iloc[-1]):
            return 0.0
        return float(data["volume"].iloc[-1] / vol_ma.iloc[-1])

    elif strategy_name == "BB_RSI_Combo":
        # RSI値をスコアとして使う（50が中立、30以下は売られすぎ、70以上は買われすぎ）
        from plugins.indicators.feature_engine import FeatureEngine
        fe = FeatureEngine()
        rsi_period = strategy.params.get("rsi_period", 14)
        df = fe.add_rsi(data.copy(), period=rsi_period)
        rsi_col = f"rsi_{rsi_period}"
        if rsi_col in df.columns and not pd.isna(df[rsi_col].iloc[-1]):
            return float(df[rsi_col].iloc[-1])
        return 50.0

    elif strategy_name == "Volume_Divergence":
        # MFI値をスコアとして使う
        from plugins.indicators.feature_engine import FeatureEngine
        fe = FeatureEngine()
        mfi_period = strategy.params.get("mfi_period", 14)
        df = fe.add_mfi(data.copy(), period=mfi_period)
        mfi_col = f"mfi_{mfi_period}"
        if mfi_col in df.columns and not pd.isna(df[mfi_col].iloc[-1]):
            return float(df[mfi_col].iloc[-1])
        return 50.0

    # その他の戦略はシグナル値をそのまま返す
    return float(signals.iloc[-1])


# ==============================================================
# 市場スキャン
# ==============================================================

def scan_market(market_key: str, all_params: dict, buy_only: bool = False) -> dict:
    """1市場をスキャンして結果を返す。"""
    config = MARKET_CONFIG[market_key]
    tickers = load_tickers(market_key)

    if not tickers:
        return {
            "market": market_key,
            "market_name": config["name"],
            "strategy": config["strategy_key"],
            "results": [],
            "summary": {"total": 0, "buy": 0, "sell": 0, "neutral": 0, "error": 0},
        }

    # 戦略インスタンス作成
    strategy_params = all_params.get(config["strategy_key"], {})
    strategy = config["strategy_class"](params=strategy_params)

    results = []
    for ticker in tickers:
        code = ticker["code"]
        name = ticker.get("name", code)

        result = analyze_ticker(code, name, strategy, config["data_period"])
        results.append(result)
        time.sleep(config["fetch_delay"])

    # BUYのみフィルタ
    if buy_only:
        results = [r for r in results if r["signal"] == "BUY"]

    # サマリー
    summary = {
        "total": len(tickers),
        "buy": sum(1 for r in results if r["signal"] == "BUY"),
        "sell": sum(1 for r in results if r["signal"] == "SELL"),
        "neutral": sum(1 for r in results if r["signal"] == "NEUTRAL"),
        "error": sum(1 for r in results if r["signal"] == "ERROR"),
    }

    return {
        "market": market_key,
        "market_name": config["name"],
        "strategy": config["strategy_key"],
        "results": results,
        "summary": summary,
    }


# ==============================================================
# 出力
# ==============================================================

def format_price(price: float, code: str) -> str:
    """銘柄に応じて価格をフォーマットする。"""
    if code.endswith(".T"):
        return f"¥{price:,.0f}"
    elif code.endswith("-JPY"):
        # JPY建て仮想通貨（BTC-JPY, ETH-JPY, XRP-JPY等）
        return f"¥{price:,.0f}"
    elif "BTC" in code:
        return f"${price:,.0f}"
    else:
        return f"${price:,.2f}"


def print_market_results(market_data: dict):
    """1市場の結果をテーブル表示する。"""
    config = MARKET_CONFIG[market_data["market"]]
    strategy_name = config["strategy_class"].__name__
    results = market_data["results"]
    summary = market_data["summary"]

    print(f"\n{'━'*70}")
    print(f"  {market_data['market_name']} ({strategy_name})")
    print(f"  BUY: {summary['buy']}  SELL: {summary['sell']}  NEUTRAL: {summary['neutral']}  ERROR: {summary['error']}")
    print(f"{'━'*70}")

    if not results:
        print("  データなし")
        return

    # ヘッダ
    print(f"  {'銘柄':<24} {'価格':>12} {'変動':>8} {'スコア':>8} {'シグナル':>10}")
    print(f"  {'-'*24} {'-'*12} {'-'*8} {'-'*8} {'-'*10}")

    for r in results:
        if r["error"]:
            print(f"  {r['name'][:20]:<24} {'ERROR':>12} {'':>8} {'':>8} {'ERROR':>10}")
            continue

        marker = ">>>" if r["signal"] == "BUY" else "   "
        price_str = format_price(r["price"], r["code"])
        change_str = f"{r['change_pct']:+.1f}%"
        name_display = f"{r['name'][:18]}({r['code']})"

        print(f"{marker}{name_display:<24} {price_str:>12} {change_str:>8} {r['score']:>8.1f} {r['signal']:>10}")


def print_all_results(all_data: list):
    """全市場の結果を表示する。"""
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*70}")
    print(f"  全市場統合スクリーナー [{today}]")
    print(f"{'='*70}")

    total_buy = sum(d["summary"]["buy"] for d in all_data)
    total_signals = sum(d["summary"]["total"] for d in all_data)
    print(f"  全{total_signals}銘柄中 BUYシグナル: {total_buy}件")

    for market_data in all_data:
        print_market_results(market_data)

    # BUYシグナルまとめ
    buy_signals = []
    for d in all_data:
        for r in d["results"]:
            if r["signal"] == "BUY":
                buy_signals.append({**r, "market": d["market_name"]})

    if buy_signals:
        print(f"\n{'='*70}")
        print(f"  BUYシグナル一覧（要注目）")
        print(f"{'='*70}")
        for s in buy_signals:
            price_str = format_price(s["price"], s["code"])
            print(f"  [{s['market']}] {s['name']}({s['code']}) {price_str} スコア:{s['score']:.1f}")
    else:
        print(f"\n  現在BUYシグナルの銘柄はありません。")

    print()


def save_results(all_data: list):
    """結果をJSONファイルに保存する。"""
    output = {
        "timestamp": datetime.now().isoformat(),
        "markets": all_data,
    }
    path = os.path.join(BASE_DIR, "screening_results_unified.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"  結果保存: {path}")


# ==============================================================
# フルスクリーニングモード（WF検証付き）
# ==============================================================

def run_full_screening(market_key: str, all_params: dict):
    """フルスクリーニング: バックテスト + WF検証 + watchlist更新。

    日本株は既存のjp_stock_screener.pyのロジックを使用。
    他市場は簡易バックテストでスクリーニング。
    """
    config = MARKET_CONFIG[market_key]
    tickers = load_tickers(market_key)

    if not tickers:
        print(f"  {config['name']}: ティッカーなし。スキップ。")
        return []

    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))
    strategy_params = all_params.get(config["strategy_key"], {})
    candidates = []

    print(f"\n{'='*60}")
    print(f"  フルスクリーニング: {config['name']} ({len(tickers)}銘柄)")
    print(f"{'='*60}\n")

    for i, ticker in enumerate(tickers):
        code = ticker["code"]
        name = ticker.get("name", code)
        progress = f"[{i+1}/{len(tickers)}]"

        data = fetch_data(code, period="2y")
        if data is None:
            print(f"  {progress} {name}({code}): SKIP (データ不足)")
            time.sleep(config["fetch_delay"])
            continue

        try:
            strategy = config["strategy_class"](params=strategy_params)
            result = engine.run(strategy, data, verbose=False)

            if result.sharpe_ratio > 0 and result.annual_return > 5:
                candidates.append({
                    "code": code,
                    "name": name,
                    "sharpe": round(result.sharpe_ratio, 4),
                    "annual_return": round(result.annual_return, 2),
                    "max_drawdown": round(result.max_drawdown, 2),
                    "win_rate": round(result.win_rate, 1),
                    "total_trades": result.total_trades,
                })
                print(f"  {progress} {name}({code}): CANDIDATE Sharpe={result.sharpe_ratio:.2f} Return={result.annual_return:+.1f}%")
            else:
                print(f"  {progress} {name}({code}): Sharpe={result.sharpe_ratio:.2f} Return={result.annual_return:+.1f}%")
        except Exception as e:
            print(f"  {progress} {name}({code}): ERROR ({e})")

        time.sleep(config["fetch_delay"])

    candidates.sort(key=lambda x: x["sharpe"], reverse=True)
    print(f"\n  候補: {len(candidates)}銘柄")
    return candidates


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="全市場統合スクリーナー（日本株/米国株/BTC/ゴールド）"
    )
    parser.add_argument(
        "--market", choices=["jp", "us", "btc", "gold", "fx"],
        help="特定の市場のみスキャン（デフォルト: 全市場）"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="JSON形式で出力"
    )
    parser.add_argument(
        "--buy-only", action="store_true", dest="buy_only",
        help="BUYシグナルの銘柄のみ表示"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="フルスクリーニングモード（バックテスト+WF検証付き、時間がかかる）"
    )
    parser.add_argument(
        "--save", action="store_true",
        help="結果をscreening_results_unified.jsonに保存"
    )
    args = parser.parse_args()

    # パラメータ読み込み
    all_params = load_params()

    # 対象市場を決定
    forced = bool(args.market)  # --market指定時は時間フィルタを無視
    if args.market:
        markets = [args.market]
    else:
        markets = ["jp", "us", "btc", "gold", "fx"]

    # フルスクリーニングモード
    if args.full:
        for market in markets:
            candidates = run_full_screening(market, all_params)
        return

    # 通常スキャンモード
    all_data = []
    for market in markets:
        # 市場時間フィルタ（--market指定時は強制スキャン）
        if not forced and not should_scan(market):
            print(f"  {MARKET_CONFIG[market]['name']}: 閉場中のためスキップ")
            continue
        print(f"  スキャン中: {MARKET_CONFIG[market]['name']}...", end="", flush=True)
        market_data = scan_market(market, all_params, buy_only=args.buy_only)
        all_data.append(market_data)
        print(f" 完了 (BUY: {market_data['summary']['buy']})")

    # 出力
    if args.json_output:
        output = {
            "timestamp": datetime.now().isoformat(),
            "markets": all_data,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print_all_results(all_data)

    # 結果保存
    if args.save or args.json_output:
        save_results(all_data)

    # 通知（BUYシグナルがあれば）
    try:
        from notifier import notify_scan_summary
        notify_scan_summary(all_data)
    except Exception as e:
        print(f"  [NOTIFY] 通知スキップ: {e}")


if __name__ == "__main__":
    main()

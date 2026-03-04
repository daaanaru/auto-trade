#!/usr/bin/env python3
"""
Monthly Momentum シグナル監視スクリプト

ウォークフォワード検証で合格した戦略をリアルタイム監視する。
cronで毎朝実行し、今日のシグナル（BUY / SELL / HOLD）を出力。

使い方:
    python signal_monitor.py                          # ソニー単体（デフォルト）
    python signal_monitor.py --symbol 7203.T          # 銘柄変更
    python signal_monitor.py --watchlist               # watchlist.json の全銘柄を一括監視
    python signal_monitor.py --watchlist 6758.T,7203.T # カンマ区切りで複数銘柄指定
    python signal_monitor.py --watchlist --json        # 一括監視 + JSON出力
    python signal_monitor.py --buy-only                # BUYシグナルのみ表示

cron設定例（毎朝8:50 JST）:
    50 8 * * 1-5 cd /path/to/auto-trade && python3 signal_monitor.py --watchlist >> signal_log.txt 2>&1
"""

import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd
import yfinance as yf

# auto-trade ディレクトリをパスに追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.monthly_momentum import MonthlyMomentumStrategy

# 銘柄名マッピング
SYMBOL_NAMES = {
    "6758.T": "ソニー",
    "7203.T": "トヨタ",
    "9984.T": "ソフトバンクG",
    "8306.T": "三菱UFJ",
    "4755.T": "楽天",
    "6501.T": "日立",
    "8035.T": "東京エレクトロン",
    "6902.T": "デンソー",
    "BTC-USD": "ビットコイン",
    "AAPL": "Apple",
    "NVDA": "NVIDIA",
    "SPY": "S&P500 ETF",
}


def load_watchlist() -> list:
    """watchlist.json からデフォルト監視銘柄リストを読み込む。

    銘柄名もSYMBOL_NAMESに反映する（動的に名前を補完）。

    Returns:
        [{"code": "6758.T", "name": "ソニー", "note": "..."}, ...]
    """
    watchlist_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "watchlist.json"
    )
    if not os.path.exists(watchlist_path):
        return [{"code": "6758.T", "name": "ソニー", "note": "default"}]
    with open(watchlist_path, "r") as f:
        data = json.load(f)
    symbols = data.get("symbols", [])
    # watchlist.json の名前で SYMBOL_NAMES を補完
    for item in symbols:
        if item["code"] not in SYMBOL_NAMES and item.get("name"):
            SYMBOL_NAMES[item["code"]] = item["name"]
    return symbols


def load_optimized_params() -> dict:
    """optimized_params.json から Monthly Momentum のパラメータを読み込む。"""
    params_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "optimized_params.json"
    )
    if not os.path.exists(params_path):
        return {}
    with open(params_path, "r") as f:
        all_params = json.load(f)
    return all_params.get("monthly", {})


def fetch_data(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """yfinance から日足データを取得する。

    Args:
        symbol: 銘柄コード（例: 6758.T）
        period: 取得期間（デフォルト3ヶ月 = 出来高MAの計算に十分）
    """
    df = yf.download(symbol, period=period, interval="1d", progress=False)
    if df.empty:
        raise ValueError(f"データ取得失敗: {symbol}")

    # MultiIndex対応（yfinance の仕様）
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.dropna()
    return df


def detect_position_state(signals: pd.Series) -> str:
    """直近のシグナル履歴からポジション状態を判定する。

    Returns:
        "LONG": 買いポジション保有中
        "NONE": ポジションなし
    """
    # 直近の非ゼロシグナルを探す
    nonzero = signals[signals != 0]
    if nonzero.empty:
        return "NONE"
    last_signal = nonzero.iloc[-1]
    return "LONG" if last_signal == 1 else "NONE"


def calc_momentum_score(data: pd.DataFrame, params: dict) -> float:
    """Monthly Momentum スコアを算出する。

    出来高比率（volume_ratio）をスコアとして返す。
    閾値以上ならシグナル発生、以下なら様子見。
    """
    vol_ma_period = params.get("volume_ma_period", 20)
    vol_ma = data["volume"].rolling(window=vol_ma_period).mean()
    if vol_ma.iloc[-1] == 0:
        return 0.0
    return data["volume"].iloc[-1] / vol_ma.iloc[-1]


def get_signal_label(signal_value: int) -> str:
    """シグナル値をラベルに変換する。"""
    if signal_value == 1:
        return "BUY"
    elif signal_value == -1:
        return "SELL"
    return "HOLD"


def get_recommendation(signal_label: str, position: str) -> str:
    """シグナルとポジションから推奨アクションを生成する。"""
    if signal_label == "BUY" and position == "NONE":
        return "寄成で買い"
    elif signal_label == "BUY" and position == "LONG":
        return "保有継続"
    elif signal_label == "SELL" and position == "LONG":
        return "寄成で売り（手仕舞い）"
    elif signal_label == "SELL" and position == "NONE":
        return "様子見（ポジションなし）"
    elif position == "LONG":
        return "保有継続（シグナルなし）"
    return "様子見"


def format_price(price: float, symbol: str) -> str:
    """銘柄に応じて価格をフォーマットする。"""
    if symbol.endswith(".T"):
        return f"¥{price:,.0f}"
    elif "USD" in symbol or "BTC" in symbol:
        return f"${price:,.2f}"
    return f"{price:,.2f}"


def analyze_symbol(symbol: str, params: dict) -> dict:
    """1銘柄を分析してシグナル情報を辞書で返す。

    Returns:
        {"symbol", "name", "signal", "score", "price", "position",
         "recommendation", "data_date", "day_of_month", "entry_days", "error"}
    """
    display_name = SYMBOL_NAMES.get(symbol, symbol)
    entry_days = params.get("entry_days", 3)

    try:
        data = fetch_data(symbol, period="3mo")
    except Exception as e:
        return {
            "symbol": symbol,
            "name": display_name,
            "signal": "ERROR",
            "score": 0.0,
            "price": 0.0,
            "position": "UNKNOWN",
            "recommendation": str(e),
            "data_date": "",
            "day_of_month": 0,
            "entry_days": entry_days,
            "error": str(e),
        }

    strategy = MonthlyMomentumStrategy(params=params)
    signals = strategy.generate_signals(data)

    today_signal = int(signals.iloc[-1])
    signal_label = get_signal_label(today_signal)

    prev_signals = signals.iloc[:-1]
    position = detect_position_state(prev_signals)

    score = calc_momentum_score(data, params)
    current_price = float(data["close"].iloc[-1])
    recommendation = get_recommendation(signal_label, position)
    data_date = data.index[-1].strftime("%Y-%m-%d")
    day_of_month = data.index[-1].day

    return {
        "symbol": symbol,
        "name": display_name,
        "signal": signal_label,
        "score": round(score, 2),
        "price": round(current_price, 2),
        "position": position,
        "recommendation": recommendation,
        "data_date": data_date,
        "day_of_month": day_of_month,
        "entry_days": entry_days,
        "error": None,
    }


def print_single(result: dict, params: dict, symbol: str):
    """単体モードの出力。"""
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today}] {result['name']}({symbol}) シグナル: {result['signal']}")
    print(f"  データ日付: {result['data_date']}")
    print(f"  現在価格: {format_price(result['price'], symbol)}")
    print(f"  Monthly Momentumスコア: {result['score']:+.2f} (閾値: {params.get('volume_threshold', 1.5):.1f})")
    print(f"  ポジション: {result['position']}")
    print(f"  推奨: {result['recommendation']}")

    day_of_month = result["day_of_month"]
    entry_days = result["entry_days"]
    if day_of_month <= entry_days:
        print(f"  [月初エントリー期間中] (日={day_of_month}, 上限={entry_days}日)")
    else:
        print(f"  [月初期間外] (日={day_of_month}, エントリーは月初{entry_days}日以内)")


def print_watchlist_table(results: list, buy_only: bool = False):
    """複数銘柄の一覧テーブルを出力する。"""
    today = datetime.now().strftime("%Y-%m-%d")

    filtered = results
    if buy_only:
        filtered = [r for r in results if r["signal"] == "BUY"]

    print(f"\n=== Monthly Momentum 一括監視 [{today}] ===\n")

    if not filtered:
        if buy_only:
            print("  BUYシグナルの銘柄はありません。")
        else:
            print("  監視対象銘柄がありません。")
        print()
        return

    # テーブルヘッダ
    print(f"  {'銘柄':<16} {'価格':>10} {'スコア':>8} {'シグナル':>8} {'ポジション':>10} {'推奨'}")
    print(f"  {'-'*16} {'-'*10} {'-'*8} {'-'*8} {'-'*10} {'-'*20}")

    buy_found = False
    for r in filtered:
        if r["error"]:
            print(f"  {r['name']+'('+r['symbol']+')' :<16} {'ERROR':>10} {'':>8} {'ERROR':>8} {'':>10} {r['recommendation']}")
            continue

        marker = ">>>" if r["signal"] == "BUY" else "   "
        price_str = format_price(r["price"], r["symbol"])
        name_col = f"{r['name']}({r['symbol']})"

        print(f"{marker}{name_col:<16} {price_str:>10} {r['score']:>+8.2f} {r['signal']:>8} {r['position']:>10} {r['recommendation']}")

        if r["signal"] == "BUY":
            buy_found = True

    print()

    # 月初情報（全銘柄共通なので最初の正常銘柄から取得）
    valid = [r for r in results if not r["error"]]
    if valid:
        day = valid[0]["day_of_month"]
        entry = valid[0]["entry_days"]
        if day <= entry:
            print(f"  [月初エントリー期間中] 日={day}, 上限={entry}日")
        else:
            print(f"  [月初期間外] 日={day}, エントリーは月初{entry}日以内")

    if not buy_only and buy_found:
        print("  >>> = BUYシグナル発生中")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Monthly Momentum シグナル監視"
    )
    parser.add_argument(
        "--symbol", default="6758.T",
        help="銘柄コード（デフォルト: 6758.T = ソニー）"
    )
    parser.add_argument(
        "--watchlist", nargs="?", const="default", default=None,
        help="一括監視モード。引数なし=watchlist.json、カンマ区切り=指定銘柄"
    )
    parser.add_argument(
        "--buy-only", action="store_true", dest="buy_only",
        help="BUYシグナルの銘柄のみ表示"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="JSON形式で出力（他ツール連携用）"
    )
    args = parser.parse_args()

    params = load_optimized_params()

    # --- 一括監視モード ---
    if args.watchlist is not None:
        # watchlist.json を常に読み込んで名前マッピングを補完
        load_watchlist()

        if args.watchlist == "default":
            watchlist = load_watchlist()
            symbols = [item["code"] for item in watchlist]
        else:
            # カンマ区切りで銘柄指定
            symbols = [s.strip() for s in args.watchlist.split(",") if s.strip()]

        results = []
        for sym in symbols:
            result = analyze_symbol(sym, params)
            results.append(result)

        if args.json_output:
            output = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "strategy": "Monthly Momentum",
                "params": params,
                "results": results,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print_watchlist_table(results, buy_only=args.buy_only)
        return

    # --- 単体モード ---
    result = analyze_symbol(args.symbol, params)

    if result["error"]:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    if args.json_output:
        result["date"] = datetime.now().strftime("%Y-%m-%d")
        result["params"] = params
        del result["error"]
        del result["day_of_month"]
        del result["entry_days"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_single(result, params, args.symbol)


if __name__ == "__main__":
    main()

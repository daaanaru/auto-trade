#!/usr/bin/env python3
"""
スキャル専用トレーダー（ペーパートレード）

上様の実績手法を機械化:
  - 上位足（1h）でトレンド方向を判定
  - 下位足（5m）でトレンド方向にエントリー
  - 固定RR比 2:1 で機械的にTP/SL
  - レバレッジ10倍（ペーパー検証後に調整）

使い方:
    python scalper.py                 # BTC/ETH スキャル実行
    python scalper.py --summary       # ポートフォリオ状況表示
    python scalper.py --dry-run       # シグナル確認のみ
    python scalper.py --reset         # ポートフォリオリセット
    python scalper.py --symbol BTC    # BTC のみ

資金管理:
    - scalp_portfolio.json で独立管理（スイングと分離）
    - 初期資金: 100,000 JPY
    - レバレッジ: 10倍（購買力 = 現金 x 10）
"""

import warnings
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from strategies.scalp_trend import ScalpTrendStrategy

# ==============================================================
# 設定
# ==============================================================

SCALP_PORTFOLIO_FILE = os.path.join(BASE_DIR, "scalp_portfolio.json")
SCALP_TRADE_LOG_FILE = os.path.join(BASE_DIR, "scalp_trade_log.json")
SCALP_LOG_DIR = os.path.join(BASE_DIR, "logs")

INITIAL_CAPITAL_JPY = 100_000.0
DEFAULT_LEVERAGE = 10

# 対象銘柄（ccxt Bybit用シンボル + yfinance用シンボル）
SCALP_SYMBOLS = {
    "BTC": {
        "ccxt": "BTC/USDT",
        "yf": "BTC-JPY",
        "name": "ビットコイン",
    },
    "ETH": {
        "ccxt": "ETH/USDT",
        "yf": "ETH-JPY",
        "name": "イーサリアム",
    },
}

# リスク管理
MAX_POSITION_PCT = 0.10        # 1トレードあたりポートフォリオの10%
MAX_CONCURRENT_POSITIONS = 3   # 同時保有最大3ポジション
COOLDOWN_MINUTES = 15          # 損切り後15分クールダウン

# RR比 2:1（上様の手法）
# ATRベースで動的に計算し、固定キャップで暴走防止
ATR_PERIOD = 14
SL_ATR_MULT = 1.5              # SL = 1.5 x ATR
TP_ATR_MULT = 3.0              # TP = 3.0 x ATR（RR = 3.0/1.5 = 2:1）
SL_CAP_PCT = 0.015             # SL上限: -1.5%（スキャルなので小さめ）
SL_FLOOR_PCT = 0.003           # SL下限: -0.3%
TP_CAP_PCT = 0.03              # TP上限: +3.0%
TP_FLOOR_PCT = 0.006           # TP下限: +0.6%

# ==============================================================
# ロガー設定
# ==============================================================

os.makedirs(SCALP_LOG_DIR, exist_ok=True)
logger = logging.getLogger("scalper")
logger.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(
    os.path.join(SCALP_LOG_DIR, "scalper.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

# ==============================================================
# データ取得（ccxt + yfinance フォールバック）
# ==============================================================

def fetch_ohlcv_ccxt(symbol_key: str, timeframe: str = "5m", limit: int = 200) -> Optional[pd.DataFrame]:
    """ccxt経由でBybitからOHLCVを取得する。

    Args:
        symbol_key: "BTC" or "ETH"
        timeframe: "1m", "5m", "1h", "4h" etc.
        limit: 取得本数

    Returns:
        DataFrame(columns: open, high, low, close, volume) or None
    """
    try:
        import ccxt
        exchange = ccxt.bybit({"enableRateLimit": True})
        ccxt_symbol = SCALP_SYMBOLS[symbol_key]["ccxt"]
        ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
        if not ohlcv:
            return None

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        logger.warning(f"ccxt OHLCV取得失敗 ({symbol_key} {timeframe}): {e}")
        return None


def fetch_ohlcv_yfinance(symbol_key: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """yfinanceからOHLCVを取得する（ccxtフォールバック用）。"""
    try:
        import yfinance as yf
        yf_symbol = SCALP_SYMBOLS[symbol_key]["yf"]
        df = yf.download(yf_symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.dropna()
        if len(df) < 10:
            return None
        return df
    except Exception as e:
        logger.warning(f"yfinance OHLCV取得失敗 ({symbol_key} {interval}): {e}")
        return None


def fetch_multi_timeframe(symbol_key: str) -> dict:
    """マルチタイムフレームのデータを一括取得する。

    Returns:
        {"upper": DataFrame(1h), "lower": DataFrame(5m)} or None values
    """
    # 上位足: 1h（トレンド判定用、200本 ≈ 8日分）
    upper = fetch_ohlcv_ccxt(symbol_key, timeframe="1h", limit=200)
    if upper is None:
        upper = fetch_ohlcv_yfinance(symbol_key, period="10d", interval="1h")

    # 下位足: 5m（エントリー用、200本 ≈ 17時間分）
    lower = fetch_ohlcv_ccxt(symbol_key, timeframe="5m", limit=200)
    if lower is None:
        lower = fetch_ohlcv_yfinance(symbol_key, period="5d", interval="5m")

    return {"upper": upper, "lower": lower}


# ==============================================================
# ATRベース動的SL/TP
# ==============================================================

def calc_atr(data: pd.DataFrame, period: int = ATR_PERIOD) -> Optional[float]:
    """ATR（平均真の値幅）をパーセンテージで計算する。"""
    if len(data) < period + 1:
        return None
    high = data["high"]
    low = data["low"]
    close = data["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    current_price = float(close.iloc[-1])
    if current_price == 0:
        return None
    return float(atr / current_price)  # パーセンテージ


def get_scalp_sltp(atr_pct: Optional[float]) -> dict:
    """ATRからスキャル用のSL/TP幅を計算する。"""
    if atr_pct is None or atr_pct <= 0:
        return {
            "sl_pct": -0.008,
            "tp_pct": 0.016,
        }

    sl_raw = atr_pct * SL_ATR_MULT
    tp_raw = atr_pct * TP_ATR_MULT

    sl = max(SL_FLOOR_PCT, min(sl_raw, SL_CAP_PCT))
    # RR比2:1を維持: TPはSLの2倍を基本とし、キャップ内に収める
    tp_from_rr = sl * 2.0
    tp = max(TP_FLOOR_PCT, min(tp_from_rr, TP_CAP_PCT))

    return {
        "sl_pct": -sl,
        "tp_pct": tp,
    }


# ==============================================================
# ポートフォリオ管理
# ==============================================================

def load_portfolio() -> dict:
    if os.path.exists(SCALP_PORTFOLIO_FILE):
        with open(SCALP_PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    return create_initial_portfolio()


def save_portfolio(portfolio: dict):
    portfolio["updated_at"] = datetime.now().isoformat()
    with open(SCALP_PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False)


def create_initial_portfolio() -> dict:
    portfolio = {
        "initial_capital_jpy": INITIAL_CAPITAL_JPY,
        "cash_jpy": INITIAL_CAPITAL_JPY,
        "leverage": DEFAULT_LEVERAGE,
        "positions": [],
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    save_portfolio(portfolio)
    return portfolio


def get_usdjpy_rate() -> float:
    """USD/JPYレートを取得する。"""
    try:
        import yfinance as yf
        ticker = yf.Ticker("JPY=X")
        hist = ticker.history(period="1d")
        if len(hist) > 0:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 150.0  # フォールバック


# ==============================================================
# クールダウン管理
# ==============================================================

def is_in_cooldown(symbol_key: str) -> bool:
    """直近の損切りからクールダウン期間内かチェック。"""
    if not os.path.exists(SCALP_TRADE_LOG_FILE):
        return False
    try:
        with open(SCALP_TRADE_LOG_FILE, "r") as f:
            logs = json.load(f)
        cutoff = (datetime.now() - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
        for entry in reversed(logs[-50:]):
            if entry.get("symbol_key") != symbol_key:
                continue
            if entry.get("action") in ("STOP_LOSS",) and entry.get("timestamp", "") > cutoff:
                return True
            if entry.get("timestamp", "") < cutoff:
                break
    except Exception:
        pass
    return False


# ==============================================================
# トレード実行
# ==============================================================

def execute_entry(portfolio: dict, symbol_key: str, side: str,
                  price: float, sltp: dict, dry_run: bool = False) -> dict:
    """エントリーを実行する。

    Args:
        side: "long" or "short"
        sltp: {"sl_pct": float, "tp_pct": float}
    """
    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)
    total_value = portfolio["initial_capital_jpy"] + portfolio["total_realized_pnl"]
    max_invest = total_value * MAX_POSITION_PCT

    # 現金チェック
    available = portfolio["cash_jpy"] * 0.95  # 5%バッファ
    if available < 100:
        print(f"  [SKIP] 現金不足: {portfolio['cash_jpy']:.0f} JPY")
        return portfolio

    buying_power = available * leverage
    invest_amount = min(max_invest, buying_power * 0.9)
    if invest_amount < 100:
        return portfolio

    # JPY建て価格
    info = SCALP_SYMBOLS[symbol_key]
    is_jpy = info["yf"].endswith("-JPY")
    if is_jpy:
        price_jpy = price
    else:
        fx_rate = get_usdjpy_rate()
        price_jpy = price * fx_rate

    shares = invest_amount / price_jpy
    margin = invest_amount / leverage
    commission = invest_amount * 0.001  # 0.1%手数料

    if dry_run:
        side_label = "LONG" if side == "long" else "SHORT"
        print(f"  [DRY] {side_label} {info['name']}({symbol_key}) @ {price:,.2f} | 投資: {invest_amount:,.0f} JPY | SL: {sltp['sl_pct']*100:+.1f}% TP: {sltp['tp_pct']*100:+.1f}%")
        return portfolio

    portfolio["cash_jpy"] -= (margin + commission)
    position = {
        "symbol_key": symbol_key,
        "code": info["yf"],
        "name": info["name"],
        "side": side,
        "shares": shares,
        "entry_price": price,
        "entry_date": datetime.now().isoformat(),
        "sl_pct": sltp["sl_pct"],
        "tp_pct": sltp["tp_pct"],
        "invest_jpy": invest_amount,
        "margin_jpy": margin,
        "high_since_entry": price,
        "low_since_entry": price,
    }
    portfolio["positions"].append(position)

    side_label = "LONG" if side == "long" else "SHORT"
    print(f"  [{side_label}] {info['name']}({symbol_key}) @ {price:,.2f} | 証拠金: {margin:,.0f} JPY ({leverage}x) | SL: {sltp['sl_pct']*100:+.1f}% TP: {sltp['tp_pct']*100:+.1f}%")
    logger.info(f"ENTRY {side_label} {symbol_key} @ {price:.2f} invest={invest_amount:.0f} sl={sltp['sl_pct']:.4f} tp={sltp['tp_pct']:.4f}")

    append_trade_log({
        "action": f"ENTRY_{side_label}",
        "symbol_key": symbol_key,
        "code": info["yf"],
        "name": info["name"],
        "side": side,
        "price": price,
        "shares": shares,
        "invest_jpy": round(invest_amount, 2),
        "margin_jpy": round(margin, 2),
        "sl_pct": sltp["sl_pct"],
        "tp_pct": sltp["tp_pct"],
        "timestamp": datetime.now().isoformat(),
    })

    # Discord通知
    try:
        from notifier import send_discord_embed
        send_discord_embed(
            title=f"[Scalp] {side_label} {info['name']}",
            description=f"@ {price:,.2f} | 証拠金: {margin:,.0f} JPY\nSL: {sltp['sl_pct']*100:+.1f}% / TP: {sltp['tp_pct']*100:+.1f}%",
            color=0x00FF00 if side == "long" else 0xFF6600,
            username="scalper",
        )
    except Exception:
        pass

    return portfolio


def check_exit(portfolio: dict) -> list:
    """全ポジションのSL/TPをチェックし、決済対象を返す。"""
    exits = []
    for pos in portfolio["positions"]:
        symbol_key = pos["symbol_key"]
        info = SCALP_SYMBOLS.get(symbol_key)
        if not info:
            continue

        # 現在価格取得（5m足の最新close）
        data = fetch_ohlcv_ccxt(symbol_key, timeframe="5m", limit=5)
        if data is None:
            data = fetch_ohlcv_yfinance(symbol_key, period="1d", interval="5m")
        if data is None or len(data) == 0:
            continue

        current_price = float(data["close"].iloc[-1])
        entry_price = pos["entry_price"]
        side = pos["side"]

        if side == "long":
            pnl_pct = (current_price - entry_price) / entry_price
            # 高値更新
            if current_price > pos.get("high_since_entry", entry_price):
                pos["high_since_entry"] = current_price
        else:  # short
            pnl_pct = (entry_price - current_price) / entry_price
            if current_price < pos.get("low_since_entry", entry_price):
                pos["low_since_entry"] = current_price

        # TP達成
        if pnl_pct >= pos["tp_pct"]:
            exits.append({
                "pos": pos,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "reason": "TAKE_PROFIT",
            })
        # SL到達
        elif pnl_pct <= pos["sl_pct"]:
            exits.append({
                "pos": pos,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "reason": "STOP_LOSS",
            })

    return exits


def execute_exit(portfolio: dict, exit_info: dict) -> dict:
    """ポジションを決済する。"""
    pos = exit_info["pos"]
    current_price = exit_info["current_price"]
    pnl_pct = exit_info["pnl_pct"]
    reason = exit_info["reason"]
    side = pos["side"]

    invest_jpy = pos["invest_jpy"]
    pnl_jpy = invest_jpy * pnl_pct
    commission = abs(invest_jpy) * 0.001

    # 現金戻し: 証拠金 + 損益 - 手数料
    margin = pos["margin_jpy"]
    portfolio["cash_jpy"] += margin + pnl_jpy - commission
    portfolio["total_realized_pnl"] += pnl_jpy
    portfolio["total_trades"] += 1
    if pnl_jpy >= 0:
        portfolio["wins"] += 1
    else:
        portfolio["losses"] += 1

    # ポジション削除（entry_dateで一意特定。is比較はJSON復元後に機能しない）
    entry_date = pos.get("entry_date", "")
    symbol_key_del = pos.get("symbol_key", "")
    portfolio["positions"] = [
        p for p in portfolio["positions"]
        if not (p.get("entry_date") == entry_date and p.get("symbol_key") == symbol_key_del)
    ]

    side_label = side.upper()
    symbol_key = pos["symbol_key"]
    name = pos["name"]
    print(f"  [EXIT/{reason}] {name}({symbol_key}) {side_label} | PnL: {pnl_jpy:+,.0f} JPY ({pnl_pct*100:+.1f}%)")
    logger.info(f"EXIT {reason} {symbol_key} {side_label} pnl={pnl_jpy:+.0f} ({pnl_pct:+.4f})")

    append_trade_log({
        "action": reason,
        "symbol_key": symbol_key,
        "code": pos["code"],
        "name": name,
        "side": side,
        "entry_price": pos["entry_price"],
        "exit_price": current_price,
        "pnl_jpy": round(pnl_jpy, 2),
        "pnl_pct": round(pnl_pct, 4),
        "shares": pos["shares"],
        "timestamp": datetime.now().isoformat(),
    })

    # Discord通知
    try:
        from notifier import send_discord_embed
        color = 0x00FF00 if pnl_jpy >= 0 else 0xFF0000
        send_discord_embed(
            title=f"[Scalp] {reason} {name}",
            description=f"{side_label} | PnL: {pnl_jpy:+,.0f} JPY ({pnl_pct*100:+.1f}%)",
            color=color,
            username="scalper",
        )
    except Exception:
        pass

    return portfolio


# ==============================================================
# トレードログ
# ==============================================================

def append_trade_log(entry: dict):
    log = []
    if os.path.exists(SCALP_TRADE_LOG_FILE):
        try:
            with open(SCALP_TRADE_LOG_FILE, "r") as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []
    log.append(entry)
    # 直近2000件のみ保持
    if len(log) > 2000:
        log = log[-2000:]
    with open(SCALP_TRADE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ==============================================================
# メインロジック
# ==============================================================

def scan_and_trade(portfolio: dict, symbols: list = None,
                   dry_run: bool = False) -> dict:
    """全銘柄をスキャンし、条件に合えばエントリー/エグジットする。"""
    if symbols is None:
        symbols = list(SCALP_SYMBOLS.keys())

    strategy = ScalpTrendStrategy()
    now = datetime.now()
    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)

    print(f"\n{'='*60}")
    print(f"  スキャルパー [{now.strftime('%Y-%m-%d %H:%M')}]")
    print(f"  資金: {portfolio['cash_jpy']:,.0f} JPY | ポジション: {len(portfolio['positions'])}件 | レバ: {leverage}x")
    print(f"{'='*60}")

    # Step 1: 既存ポジションのSL/TPチェック
    print(f"\n[Step 1] SL/TP チェック...")
    exits = check_exit(portfolio)
    for ex in exits:
        if dry_run:
            print(f"  [DRY EXIT] {ex['pos']['name']} | {ex['reason']} | PnL: {ex['pnl_pct']*100:+.1f}%")
        else:
            portfolio = execute_exit(portfolio, ex)
    if not exits:
        print("  決済対象なし")

    # Step 2: 新規エントリースキャン
    print(f"\n[Step 2] エントリースキャン...")
    for symbol_key in symbols:
        if symbol_key not in SCALP_SYMBOLS:
            continue

        info = SCALP_SYMBOLS[symbol_key]

        # 同時保有上限チェック
        if len(portfolio["positions"]) >= MAX_CONCURRENT_POSITIONS:
            print(f"  ポジション上限({MAX_CONCURRENT_POSITIONS})に達しています")
            break

        # 同一銘柄の重複チェック
        if any(p["symbol_key"] == symbol_key for p in portfolio["positions"]):
            print(f"  {info['name']}({symbol_key}): 既にポジション保有中")
            continue

        # クールダウンチェック
        if is_in_cooldown(symbol_key):
            print(f"  {info['name']}({symbol_key}): クールダウン中（SL後{COOLDOWN_MINUTES}分）")
            continue

        # マルチタイムフレームデータ取得
        data = fetch_multi_timeframe(symbol_key)
        if data["upper"] is None or data["lower"] is None:
            print(f"  {info['name']}({symbol_key}): データ取得失敗")
            continue

        # 上位足トレンド判定
        trend = strategy.detect_trend(data["upper"])
        print(f"  {info['name']}({symbol_key}) | 上位足トレンド: {trend.upper()}")

        if trend == "range":
            print(f"    → レンジ相場のためスキップ")
            continue

        # 下位足シグナル判定
        signals = strategy.generate_signals(data["lower"], trend=trend)
        latest_signal = int(signals.iloc[-1])

        if latest_signal == 0:
            print(f"    → エントリーシグナルなし")
            continue

        current_price = float(data["lower"]["close"].iloc[-1])
        side = "long" if latest_signal == 1 else "short"

        # ATRベースSL/TP計算
        atr_pct = calc_atr(data["lower"])
        sltp = get_scalp_sltp(atr_pct)

        print(f"    → シグナル: {side.upper()} @ {current_price:,.2f} | ATR: {atr_pct*100:.2f}% | SL: {sltp['sl_pct']*100:+.1f}% TP: {sltp['tp_pct']*100:+.1f}%")

        # エントリー
        portfolio = execute_entry(
            portfolio, symbol_key, side, current_price, sltp, dry_run=dry_run
        )

        time.sleep(0.5)

    save_portfolio(portfolio)
    return portfolio


def print_summary(portfolio: dict):
    """ポートフォリオサマリーを表示する。"""
    total_unrealized = 0.0
    print(f"\n{'='*60}")
    print(f"  SCALP PORTFOLIO")
    print(f"  開始: {portfolio.get('created_at', 'N/A')[:10]} | レバ: {portfolio.get('leverage', DEFAULT_LEVERAGE)}x")
    print(f"{'='*60}")
    print(f"  初期資金:    {portfolio['initial_capital_jpy']:>12,.0f} JPY")
    print(f"  現金:        {portfolio['cash_jpy']:>12,.0f} JPY")
    print(f"  確定損益:    {portfolio['total_realized_pnl']:>+12,.0f} JPY")

    trades = portfolio.get("total_trades", 0)
    wins = portfolio.get("wins", 0)
    losses = portfolio.get("losses", 0)
    win_rate = (wins / trades * 100) if trades > 0 else 0

    print(f"  取引回数:    {trades} (勝: {wins} / 負: {losses})")
    print(f"  勝率:        {win_rate:.1f}%")
    print(f"  ポジション:  {len(portfolio['positions'])}/{MAX_CONCURRENT_POSITIONS}")

    if portfolio["positions"]:
        print(f"\n  {'銘柄':<12} {'L/S':<6} {'参入値':>12} {'SL%':>6} {'TP%':>6}")
        print(f"  {'-'*48}")
        for pos in portfolio["positions"]:
            side_label = pos["side"].upper()
            print(f"  {pos['name']:<12} {side_label:<6} {pos['entry_price']:>12,.2f} {pos['sl_pct']*100:>+5.1f}% {pos['tp_pct']*100:>+5.1f}%")

    total = portfolio["initial_capital_jpy"] + portfolio["total_realized_pnl"]
    ret_pct = (total / portfolio["initial_capital_jpy"] - 1) * 100
    print(f"\n  総資産:      {total:>12,.0f} JPY ({ret_pct:+.1f}%)")


# ==============================================================
# エントリーポイント
# ==============================================================

def main():
    parser = argparse.ArgumentParser(description="スキャル専用トレーダー")
    parser.add_argument("--summary", action="store_true", help="ポートフォリオ状況表示")
    parser.add_argument("--dry-run", action="store_true", help="シグナル確認のみ")
    parser.add_argument("--reset", action="store_true", help="ポートフォリオリセット")
    parser.add_argument("--symbol", type=str, help="対象銘柄 (BTC or ETH)")
    args = parser.parse_args()

    if args.reset:
        portfolio = create_initial_portfolio()
        print("ポートフォリオをリセットしました。")
        print_summary(portfolio)
        return

    portfolio = load_portfolio()

    if args.summary:
        print_summary(portfolio)
        return

    symbols = None
    if args.symbol:
        key = args.symbol.upper()
        if key in SCALP_SYMBOLS:
            symbols = [key]
        else:
            print(f"不明な銘柄: {args.symbol}。対応銘柄: {', '.join(SCALP_SYMBOLS.keys())}")
            sys.exit(1)

    portfolio = scan_and_trade(portfolio, symbols=symbols, dry_run=args.dry_run)
    print_summary(portfolio)


if __name__ == "__main__":
    main()

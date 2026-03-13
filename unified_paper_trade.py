#!/usr/bin/env python3
"""
全市場統合ペーパートレーダー

日本株・米国株・BTC・ゴールドを横断してペーパートレードを行う。
テクニカル分析（7戦略）+ ファンダメンタル分析（yfinance企業情報）で判断。

初期資金: $200（約30,000円）
期限: 2026/03/30

使い方:
    python unified_paper_trade.py                # 全市場巡回→自動トレード
    python unified_paper_trade.py --summary      # ポートフォリオ状況表示
    python unified_paper_trade.py --report        # 日次レポート生成
    python unified_paper_trade.py --reset         # ポートフォリオリセット
    python unified_paper_trade.py --market jp     # 日本株のみ巡回
    python unified_paper_trade.py --dry-run       # シグナル確認のみ（売買しない）

cron設定例（毎日7:00に全市場巡回）:
    0 7 * * * cd /path/to/auto-trade && python3 unified_paper_trade.py >> logs/unified-paper-trade.log 2>&1
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy
from market_fundamental import get_market_fundamental_score

# ==============================================================
# 設定
# ==============================================================

PORTFOLIO_FILE = os.path.join(BASE_DIR, "paper_portfolio.json")
PORTFOLIO_LOG_FILE = os.path.join(BASE_DIR, "paper_portfolio_log.json")
TRADE_LOG_FILE = os.path.join(BASE_DIR, "paper_trade_log.json")
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")
DAILY_REPORT_FILE = os.path.join(BASE_DIR, "daily_report.md")

INITIAL_CAPITAL_USD = 2000.0
INITIAL_CAPITAL_JPY = 300000.0  # 約$2000（検証用）

# リスク管理ルール
MAX_POSITION_PCT = 0.04        # 1銘柄あたりポートフォリオの4%（50枠分散）
MAX_POSITIONS = 50             # 同時保有最大50ポジション（5市場 x 10枠）
MAX_POSITIONS_PER_MARKET = 10  # 1市場あたり最大10ポジション
STOP_LOSS_PCT = -0.03          # -3%で強制クローズ（ロング用。ショートは+3%で発動）
TAKE_PROFIT_PCT_1 = 0.03       # +3%で1/3利確（第1段階）
TAKE_PROFIT_PCT_2 = 0.10       # +10%でさらに1/3利確（第2段階）
TRAILING_STOP_PCT = 0.03       # 高値から-3%でトレーリングストップ（残り全決済）
COMMISSION_RATE = 0.001        # 手数料0.1%
DEFAULT_LEVERAGE = 2           # デフォルトのレバレッジ倍率（購買力 = 現金 x この倍率）
CASH_RESERVE_PCT = 0.10        # 現金保留率10%（総資産の10%を現金として常に確保）
FORCE_EXIT_DAYS = 7            # 7日保有で強制決済（塩漬け防止）
EARLY_TRAILING_TRIGGER = 0.02  # +2%到達でトレーリングストップ早期発動
EARLY_TRAILING_STOP = 0.02     # 早期トレーリング: 高値から-2%で全決済

# 為替レート取得
def get_usdjpy_rate() -> float:
    """USDJPY為替レートを取得する。取得失敗時は150.0を返す。"""
    try:
        data = yf.download("USDJPY=X", period="1d", progress=False)
        if data is not None and len(data) > 0:
            close_series = data["Close"]
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]
            rate = float(close_series.iloc[-1])
            if 100 < rate < 200:  # 妥当性チェック
                return rate
    except Exception:
        pass
    return 150.0  # フォールバック

def _is_usd_ticker(code: str) -> bool:
    """ティッカーがUSD建てかどうかを判定する。"""
    if code.endswith(".T"):
        return False  # 東証銘柄はJPY
    if code.endswith("=X"):
        # FX pairs: JPY建てペア(USDJPY=X, EURJPY=X等)は変換不要
        return "JPY" not in code
    if code.endswith("-JPY"):
        return False
    # 上記以外（米国株、BTC-USD、GLD等）はUSD建て
    return True


# USD建てとして変換すべきmarketのセット
# btcはJPY建てペア（BTC-JPY等）を使うので除外
_USD_MARKETS = {"us", "gold"}


def price_to_jpy(price: float, market: str, fx_rate: float = None,
                 code: str = None) -> float:
    """市場に応じて価格を円建てに変換する。

    判定ロジック（二重チェック）:
    1. market が us/gold/btc ならUSD→JPY変換
    2. code が指定されていれば、ティッカー名からも通貨を推定
    3. どちらか一方でもUSD判定ならば変換する（安全側に倒す）
    """
    need_convert = market in _USD_MARKETS
    if code and not need_convert:
        need_convert = _is_usd_ticker(code)

    if need_convert:
        if fx_rate is None:
            fx_rate = get_usdjpy_rate()
        return price * fx_rate
    return price  # JPY建て銘柄はそのまま

# 市場→戦略マッピング
MARKET_STRATEGIES = {
    "jp": {"class": MonthlyMomentumStrategy, "param_key": "monthly", "period": "3mo"},
    "us": {"class": BBRSIComboStrategy, "param_key": "bb_rsi", "period": "3mo"},
    "btc": {"class": VolumeDivergenceStrategy, "param_key": "vol_div", "period": "1y"},
    "gold": {"class": BBRSIComboStrategy, "param_key": "bb_rsi", "period": "3mo"},
    "fx": {"class": BBRSIComboStrategy, "param_key": "bb_rsi", "period": "3mo"},
}

# スキャン対象（軽量版: 各市場の代表銘柄）
SCAN_TICKERS = {
    "jp": [
        {"code": "6758.T", "name": "ソニー"},
        {"code": "9843.T", "name": "ニトリ"},
        {"code": "4043.T", "name": "トクヤマ"},
        {"code": "5301.T", "name": "東海カーボン"},
        {"code": "9984.T", "name": "ソフトバンクG"},
        {"code": "7974.T", "name": "任天堂"},
        {"code": "8306.T", "name": "三菱UFJ"},
        {"code": "4005.T", "name": "住友化学"},
        {"code": "2801.T", "name": "キッコーマン"},
        {"code": "8001.T", "name": "伊藤忠"},
    ],
    "us": [
        {"code": "AAPL", "name": "Apple"},
        {"code": "NVDA", "name": "NVIDIA"},
        {"code": "MSFT", "name": "Microsoft"},
        {"code": "TSLA", "name": "Tesla"},
        {"code": "AMZN", "name": "Amazon"},
        {"code": "META", "name": "Meta"},
        {"code": "GOOGL", "name": "Alphabet"},
        {"code": "JPM", "name": "JPMorgan"},
        {"code": "XOM", "name": "ExxonMobil"},
        {"code": "LLY", "name": "Eli Lilly"},
    ],
    "btc": [
        {"code": "BTC-JPY",  "name": "ビットコイン"},
        {"code": "ETH-JPY",  "name": "イーサリアム"},
        {"code": "XRP-JPY",  "name": "リップル"},
        {"code": "XLM-JPY",  "name": "ステラルーメン"},
        {"code": "MONA-JPY", "name": "モナコイン"},
    ],
    "gold": [
        {"code": "GLD", "name": "Gold ETF"},
    ],
    "fx": [
        {"code": "EURUSD=X", "name": "ユーロ/ドル"},
        {"code": "GBPUSD=X", "name": "ポンド/ドル"},
        {"code": "USDJPY=X", "name": "ドル/円"},
        {"code": "AUDUSD=X", "name": "豪ドル/ドル"},
        {"code": "USDCHF=X", "name": "ドル/スイスフラン"},
        {"code": "EURJPY=X", "name": "ユーロ/円"},
        {"code": "GBPJPY=X", "name": "ポンド/円"},
    ],
}


# ==============================================================
# データ取得
# ==============================================================

def fetch_data(symbol: str, period: str = "3mo", min_rows: int = 30,
               interval: str = "1d", max_retries: int = 3) -> Optional[pd.DataFrame]:
    """yfinance からデータを取得する。リトライ付き。"""
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


def get_fundamental_score(symbol: str) -> dict:
    """yfinanceからファンダメンタル情報を取得してスコア化する。

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
        正のスコア = ファンダメンタル良好、負 = 悪い
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        score = 0.0
        reasons = []

        # PER（株価収益率）
        pe = info.get("trailingPE") or info.get("forwardPE")
        if pe:
            if pe < 15:
                score += 0.3
                reasons.append(f"PER低い({pe:.1f})")
            elif pe > 40:
                score -= 0.2
                reasons.append(f"PER高い({pe:.1f})")

        # PBR（株価純資産倍率）
        pb = info.get("priceToBook")
        if pb:
            if pb < 1.5:
                score += 0.2
                reasons.append(f"PBR割安({pb:.1f})")
            elif pb > 5:
                score -= 0.1
                reasons.append(f"PBR割高({pb:.1f})")

        # 配当利回り（yfinanceはパーセント値で返す: 2.07 = 2.07%）
        div_yield = info.get("dividendYield")
        if div_yield and div_yield > 3.0:
            score += 0.2
            reasons.append(f"高配当({div_yield:.2f}%)")

        # 売上成長率
        rev_growth = info.get("revenueGrowth")
        if rev_growth:
            if rev_growth > 0.1:
                score += 0.3
                reasons.append(f"売上成長({rev_growth*100:.0f}%)")
            elif rev_growth < -0.1:
                score -= 0.3
                reasons.append(f"売上減少({rev_growth*100:.0f}%)")

        # 時価総額（小型株は避ける）
        market_cap = info.get("marketCap")
        if market_cap and market_cap < 1e9:  # 10億未満
            score -= 0.1
            reasons.append("小型株")

        # 52週高値からの距離（高値圏は警戒）
        fifty_two_high = info.get("fiftyTwoWeekHigh")
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if fifty_two_high and current_price:
            ratio = current_price / fifty_two_high
            if ratio > 0.95:
                score -= 0.1
                reasons.append("52週高値圏")
            elif ratio < 0.7:
                score += 0.1
                reasons.append("52週安値圏")

        # スコアを-1~1にクリップ
        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 2),
            "reason": " / ".join(reasons) if reasons else "情報不足",
            "data": {
                "pe": pe,
                "pb": pb,
                "div_yield": f"{div_yield:.1f}%" if div_yield else None,
                "rev_growth": f"{rev_growth*100:.0f}%" if rev_growth else None,
                "market_cap": market_cap,
            },
        }
    except Exception as e:
        return {"score": -1.0, "reason": f"取得失敗: {e}", "data": {}}


# ==============================================================
# 売買ログ記録
# ==============================================================

def append_trade_log(entry: dict):
    """売買履歴をpaper_trade_log.jsonに追記する。"""
    log = []
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                log = json.load(f)
        except (json.JSONDecodeError, ValueError):
            log = []
    log.append(entry)
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


def append_trade_history(record: dict):
    """全トレード履歴をtrade_history.jsonに永続保存する。

    スキーマ: {"trades": [{"timestamp", "symbol", "action", "price", "shares", "pnl", "strategy", ...}]}
    ファイルが存在しない場合は自動生成する。
    """
    data = {"trades": []}
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                data = json.load(f)
            if "trades" not in data:
                data["trades"] = []
        except (json.JSONDecodeError, ValueError):
            data = {"trades": []}
    data["trades"].append(record)
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# ポートフォリオ管理
# ==============================================================

def load_portfolio() -> dict:
    """ポートフォリオ状態を読み込む。

    後方互換: 古いデータに leverage や side がない場合はデフォルト値を補完する。
    これにより、ショート・レバレッジ対応前のデータでもエラーなく動く。
    """
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r") as f:
            portfolio = json.load(f)

        # レバレッジ情報がなければデフォルト値を追加
        if "leverage" not in portfolio:
            portfolio["leverage"] = DEFAULT_LEVERAGE

        # 各ポジションに side がなければ "long" を付与（既存データとの互換）
        for pos in portfolio.get("positions", []):
            if "side" not in pos:
                pos["side"] = "long"

        # 決済済みトレードにも side がなければ "long" を付与
        for trade in portfolio.get("closed_trades", []):
            if "side" not in trade:
                trade["side"] = "long"

        return portfolio
    return create_initial_portfolio()


def save_portfolio(portfolio: dict):
    """ポートフォリオを保存する。"""
    portfolio["last_updated"] = datetime.now().isoformat()
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False, default=str)


def create_initial_portfolio() -> dict:
    return {
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "initial_capital_jpy": INITIAL_CAPITAL_JPY,
        "cash_jpy": INITIAL_CAPITAL_JPY,
        "leverage": DEFAULT_LEVERAGE,  # レバレッジ倍率（購買力 = 現金 x この値）
        "positions": [],  # [{"code","name","market","shares","entry_price","entry_date","strategy","side"}]
        "closed_trades": [],  # 決済済みの取引記録
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
    }


def load_portfolio_log() -> list:
    if os.path.exists(PORTFOLIO_LOG_FILE):
        with open(PORTFOLIO_LOG_FILE, "r") as f:
            return json.load(f)
    return []


def save_portfolio_log(log: list):
    with open(PORTFOLIO_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# トレード実行
# ==============================================================

def get_position_value(position: dict, fx_rate: float = None) -> float:
    """ポジションの現在価値を円建てで取得する。

    ロング: 現在価格 x 株数（そのまま）
    ショート: エントリー時の投資額 + 含み損益
      = (entry_price x shares) + (entry_price - current_price) x shares
      = entry_price x shares x 2 - current_price x shares
      ※ショートは「借りた株を売って後で買い戻す」ので、
        価格が下がれば利益、上がれば損失になる。
    """
    market = position.get("market", "jp")
    side = position.get("side", "long")
    data = fetch_data(position["code"], period="5d", min_rows=1)
    if data is None:
        price = position["entry_price"]
    else:
        price = float(data["close"].iloc[-1].item())

    price_jpy = price_to_jpy(price, market, fx_rate, code=position["code"])
    entry_price_jpy = price_to_jpy(position["entry_price"], market, fx_rate, code=position["code"])

    if side == "short":
        # ショートの現在価値 = 証拠金（エントリー時の価値）+ 含み損益
        # 含み損益 = (参入価格 - 現在価格) x 株数（価格下落で利益）
        return (entry_price_jpy * 2 - price_jpy) * position["shares"]
    else:
        # ロング: 普通に現在価格 x 株数
        return price_jpy * position["shares"]


def check_stop_loss_take_profit(portfolio: dict) -> list:
    """全ポジションの損切り・利確・トレーリングストップ・強制決済をチェック。

    改善点（v2）:
      - 開場中は15分足で最新価格を取得（日中の変動を捉える）
      - 第1段階利確を+3%に引き下げ（回転率向上）
      - 保有7日超で強制決済（塩漬け防止）
      - +2%到達で早期トレーリングストップ発動
    """
    from market_hours import is_market_open

    actions = []
    now = datetime.now()

    for pos in portfolio["positions"]:
        market = pos.get("market", "jp")

        # 開場中は15分足で最新価格を取得、閉場中は日足
        if is_market_open(market):
            data = fetch_data(pos["code"], period="5d", min_rows=1, interval="15m")
        else:
            data = fetch_data(pos["code"], period="5d", min_rows=1, interval="1d")

        if data is None:
            continue
        current_price = float(data["close"].iloc[-1].item())
        side = pos.get("side", "long")

        # 価格変動率
        price_change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

        # ショートの場合、損益の方向が逆
        if side == "short":
            pnl_pct = -price_change_pct
        else:
            pnl_pct = price_change_pct

        # --- トレーリングストップ用: 高値/安値の更新 ---
        if side == "long":
            prev_peak = pos.get("trailing_peak", pos["entry_price"])
            new_peak = max(prev_peak, current_price)
            pos["trailing_peak"] = new_peak
            drawdown_from_peak = (current_price - new_peak) / new_peak
        else:
            prev_trough = pos.get("trailing_trough", pos["entry_price"])
            new_trough = min(prev_trough, current_price)
            pos["trailing_trough"] = new_trough
            drawdown_from_peak = (current_price - new_trough) / new_trough

        tp_stage = pos.get("tp_stage", 0)

        # --- 保有日数チェック ---
        entry_date = datetime.fromisoformat(pos["entry_date"])
        holding_days = (now - entry_date).days

        # === 判定ロジック（優先度順） ===

        # 1. 損切り（-3%以下で全決済）
        if pnl_pct <= STOP_LOSS_PCT:
            actions.append({
                "action": "STOP_LOSS",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": pos["shares"],
            })

        # 2. 強制決済（7日保有で塩漬け防止）
        elif holding_days >= FORCE_EXIT_DAYS:
            actions.append({
                "action": "FORCE_EXIT",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": pos["shares"],
                "holding_days": holding_days,
            })

        # 3. トレーリングストップ（利確済みポジション: 高値/安値から3%逆行で全決済）
        elif tp_stage >= 1 and (
            (side == "long" and drawdown_from_peak <= -TRAILING_STOP_PCT) or
            (side == "short" and drawdown_from_peak >= TRAILING_STOP_PCT)
        ):
            actions.append({
                "action": "TRAILING_STOP",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": pos["shares"],
            })

        # 4. 早期トレーリングストップ（+2%到達後、高値から-2%で全決済）
        elif tp_stage == 0 and pnl_pct >= EARLY_TRAILING_TRIGGER and (
            (side == "long" and drawdown_from_peak <= -EARLY_TRAILING_STOP) or
            (side == "short" and drawdown_from_peak >= EARLY_TRAILING_STOP)
        ):
            actions.append({
                "action": "EARLY_TRAILING_STOP",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": pos["shares"],
            })

        # 5. 第1段階利確（+3%で1/3利確）
        elif tp_stage == 0 and pnl_pct >= TAKE_PROFIT_PCT_1:
            shares_to_sell = pos["shares"] / 3
            actions.append({
                "action": "TAKE_PROFIT_1",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": shares_to_sell,
                "_tp_stage_after": 1,
            })

        # 6. 第2段階利確（+10%でさらに1/3利確）
        elif tp_stage == 1 and pnl_pct >= TAKE_PROFIT_PCT_2:
            shares_to_sell = pos["shares"] / 2
            actions.append({
                "action": "TAKE_PROFIT_2",
                "code": pos["code"], "name": pos["name"], "side": side,
                "entry_price": pos["entry_price"], "current_price": current_price,
                "pnl_pct": pnl_pct, "shares": shares_to_sell,
                "_tp_stage_after": 2,
            })

    return actions


def execute_buy(portfolio: dict, code: str, name: str, market: str,
                price: float, strategy: str, fundamental: dict) -> dict:
    """ロング（買い）エントリーを実行する。

    「この銘柄は値上がりしそう」→ 買って持つ。上がったら売って利益。
    """
    # 為替レート取得（USD建て銘柄は円換算が必要）
    fx_rate = get_usdjpy_rate() if (market in _USD_MARKETS or _is_usd_ticker(code)) else 1.0
    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)

    # ポートフォリオ総額の20%が1銘柄の上限（レバレッジ後の実効値で計算）
    total_value = portfolio["cash_jpy"] + sum(
        get_position_value(p, fx_rate) for p in portfolio["positions"]
    )
    max_invest = total_value * MAX_POSITION_PCT

    # 既にポジションがある場合はスキップ（同一銘柄のロング+ショート同時保有禁止）
    if any(p["code"] == code for p in portfolio["positions"]):
        return portfolio

    # 同時保有上限チェック（全体 + 市場別）
    if len(portfolio["positions"]) >= MAX_POSITIONS:
        return portfolio
    market_positions = sum(1 for p in portfolio["positions"] if p.get("market") == market)
    if market_positions >= MAX_POSITIONS_PER_MARKET:
        return portfolio

    # 現金保留率ガード: 総資産の10%を現金として確保
    cash_reserve = total_value * CASH_RESERVE_PCT
    available_cash = max(0, portfolio["cash_jpy"] - cash_reserve)
    if available_cash < 100:
        print(f"  現金保留率ガード発動: 現金{portfolio['cash_jpy']:.0f}円 < 保留{cash_reserve:.0f}円+100円")
        return portfolio

    # 購買力 = 使用可能現金 x レバレッジ倍率
    buying_power = available_cash * leverage
    # 投資額を決定（購買力の90%まで、かつ1銘柄上限以内）
    invest_amount = min(max_invest, buying_power * 0.9)
    if invest_amount < 100:  # 最低100円
        return portfolio

    # 円建て投資額を現地通貨の価格で割って株数を算出
    price_jpy = price_to_jpy(price, market, fx_rate, code=code)
    shares = invest_amount / price_jpy
    cost = invest_amount * COMMISSION_RATE

    # 実際に現金から引かれるのは「証拠金」=投資額/レバレッジ + 手数料
    margin = invest_amount / leverage
    portfolio["cash_jpy"] -= (margin + cost)
    portfolio["positions"].append({
        "code": code,
        "name": name,
        "market": market,
        "shares": shares,
        "entry_price": price,
        "entry_date": datetime.now().isoformat(),
        "strategy": strategy,
        "side": "long",  # ロングポジション
        "fundamental_score": fundamental.get("score", 0),
        "fundamental_reason": fundamental.get("reason", ""),
    })

    print(f"  [BUY/LONG] {name}({code}) @ {price:,.2f} x {shares:.4f} = {invest_amount:,.0f} JPY (証拠金: {margin:,.0f} JPY, {leverage}x)")
    print(f"        ファンダ: {fundamental.get('reason', 'N/A')}")

    append_trade_log({
        "action": "BUY", "side": "long", "code": code, "name": name,
        "market": market, "price": price, "shares": shares,
        "invest_jpy": round(invest_amount, 2), "strategy": strategy,
        "fundamental_score": fundamental.get("score", 0),
        "timestamp": datetime.now().isoformat(),
    })

    append_trade_history({
        "timestamp": datetime.now().isoformat(),
        "symbol": code,
        "action": "BUY",
        "price": price,
        "shares": round(shares, 6),
        "pnl": None,
        "strategy": strategy,
        "side": "long",
        "market": market,
        "name": name,
    })

    # 通知
    try:
        from notifier import notify_buy_signal
        notify_buy_signal(code, name, market, price, strategy, 0, fundamental)
    except Exception:
        pass

    return portfolio


def execute_short(portfolio: dict, code: str, name: str, market: str,
                  price: float, strategy: str, fundamental: dict) -> dict:
    """ショート（空売り）エントリーを実行する。

    「この銘柄は値下がりしそう」→ 借りて売る。下がったら買い戻して利益。
    ショートの損益 = (参入価格 - 現在価格) x 数量
    """
    # 為替レート取得
    fx_rate = get_usdjpy_rate() if (market in _USD_MARKETS or _is_usd_ticker(code)) else 1.0
    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)

    # ポートフォリオ総額の20%が1銘柄の上限（レバレッジ後の実効値）
    total_value = portfolio["cash_jpy"] + sum(
        get_position_value(p, fx_rate) for p in portfolio["positions"]
    )
    max_invest = total_value * MAX_POSITION_PCT

    # 既にポジションがある場合はスキップ（同一銘柄のロング+ショート同時保有禁止）
    if any(p["code"] == code for p in portfolio["positions"]):
        return portfolio

    # 同時保有上限チェック（全体 + 市場別）
    if len(portfolio["positions"]) >= MAX_POSITIONS:
        return portfolio
    market_positions = sum(1 for p in portfolio["positions"] if p.get("market") == market)
    if market_positions >= MAX_POSITIONS_PER_MARKET:
        return portfolio

    # 現金保留率ガード: 総資産の10%を現金として確保
    cash_reserve = total_value * CASH_RESERVE_PCT
    available_cash = max(0, portfolio["cash_jpy"] - cash_reserve)
    if available_cash < 100:
        print(f"  現金保留率ガード発動(SHORT): 現金{portfolio['cash_jpy']:.0f}円 < 保留{cash_reserve:.0f}円+100円")
        return portfolio

    # 購買力 = 使用可能現金 x レバレッジ倍率
    buying_power = available_cash * leverage
    invest_amount = min(max_invest, buying_power * 0.9)
    if invest_amount < 100:
        return portfolio

    # 円建て投資額を現地通貨の価格で割って株数を算出
    price_jpy = price_to_jpy(price, market, fx_rate, code=code)
    shares = invest_amount / price_jpy
    cost = invest_amount * COMMISSION_RATE

    # ショートの証拠金 = 投資額/レバレッジ（レバレッジで拡大した分の担保）
    margin = invest_amount / leverage
    portfolio["cash_jpy"] -= (margin + cost)
    portfolio["positions"].append({
        "code": code,
        "name": name,
        "market": market,
        "shares": shares,
        "entry_price": price,
        "entry_date": datetime.now().isoformat(),
        "strategy": strategy,
        "side": "short",  # ショートポジション
        "fundamental_score": fundamental.get("score", 0),
        "fundamental_reason": fundamental.get("reason", ""),
    })

    print(f"  [SHORT] {name}({code}) @ {price:,.2f} x {shares:.4f} = {invest_amount:,.0f} JPY (証拠金: {margin:,.0f} JPY, {leverage}x)")
    print(f"        ファンダ: {fundamental.get('reason', 'N/A')}")

    append_trade_log({
        "action": "SHORT", "side": "short", "code": code, "name": name,
        "market": market, "price": price, "shares": shares,
        "invest_jpy": round(invest_amount, 2), "strategy": strategy,
        "fundamental_score": fundamental.get("score", 0),
        "timestamp": datetime.now().isoformat(),
    })

    append_trade_history({
        "timestamp": datetime.now().isoformat(),
        "symbol": code,
        "action": "SELL",
        "price": price,
        "shares": round(shares, 6),
        "pnl": None,
        "strategy": strategy,
        "side": "short",
        "market": market,
        "name": name,
    })

    # 通知
    try:
        from notifier import notify_sell_signal
        notify_sell_signal(code, name, price, 0, 0, "SHORT_ENTRY")
    except Exception:
        pass

    return portfolio


def execute_sell(portfolio: dict, code: str, price: float, shares: float,
                 reason: str) -> dict:
    """ポジションをクローズする（ロングの売り決済、またはショートの買い戻し決済）。

    ロングの場合: 株を売って現金化。損益 = (売値 - 買値) x 株数
    ショートの場合: 借りた株を買い戻して返却。損益 = (売値(参入時) - 買値(決済時)) x 株数
    """
    pos = None
    pos_idx = -1
    for i, p in enumerate(portfolio["positions"]):
        if p["code"] == code:
            pos = p
            pos_idx = i
            break
    if pos is None:
        return portfolio

    market = pos.get("market", "jp")
    side = pos.get("side", "long")
    pos_code = pos.get("code", code)
    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)
    fx_rate = get_usdjpy_rate() if (market in _USD_MARKETS or _is_usd_ticker(pos_code)) else 1.0

    sell_shares = min(shares, pos["shares"])

    # 損益計算（side に応じて方向が変わる）
    if side == "short":
        # ショート: 参入価格で売って、現在価格で買い戻す
        # 損益 = (参入価格 - 現在価格) x 株数（値下がりで利益）
        gross_pnl_local = (pos["entry_price"] - price) * sell_shares
    else:
        # ロング: 現在価格で売る
        # 損益 = (現在価格 - 参入価格) x 株数（値上がりで利益）
        gross_pnl_local = (price - pos["entry_price"]) * sell_shares

    gross_pnl = price_to_jpy(gross_pnl_local, market, fx_rate, code=pos_code)
    close_value_jpy = price_to_jpy(price * sell_shares, market, fx_rate, code=pos_code)
    cost = close_value_jpy * COMMISSION_RATE
    net_pnl = gross_pnl - cost

    # 現金の回収（証拠金の返還 + 損益）
    if side == "short":
        # ショート決済: 証拠金を返してもらい、損益を加算
        entry_value_jpy = price_to_jpy(pos["entry_price"] * sell_shares, market, fx_rate, code=pos_code)
        margin_return = entry_value_jpy / leverage  # 拘束していた証拠金の返還
        portfolio["cash_jpy"] += (margin_return + net_pnl)
    else:
        # ロング決済: エントリー時の証拠金を返還 + 損益を加算
        entry_value_jpy = price_to_jpy(pos["entry_price"] * sell_shares, market, fx_rate, code=pos_code)
        margin_return = entry_value_jpy / leverage  # エントリー時ベースの証拠金返還
        portfolio["cash_jpy"] += (margin_return + net_pnl)

    portfolio["total_realized_pnl"] += net_pnl
    portfolio["total_trades"] += 1
    if net_pnl > 0:
        portfolio["winning_trades"] += 1
    else:
        portfolio["losing_trades"] += 1

    side_label = "LONG" if side == "long" else "SHORT"
    portfolio["closed_trades"].append({
        "code": code,
        "name": pos["name"],
        "market": pos["market"],
        "side": side,
        "entry_price": pos["entry_price"],
        "exit_price": price,
        "shares": sell_shares,
        "net_pnl_jpy": round(net_pnl, 2),
        "entry_date": pos["entry_date"],
        "exit_date": datetime.now().isoformat(),
        "reason": reason,
        "strategy": pos["strategy"],
    })

    # ポジションの残りを更新
    remaining = pos["shares"] - sell_shares
    if remaining < 0.00001:
        portfolio["positions"].pop(pos_idx)
    else:
        portfolio["positions"][pos_idx]["shares"] = remaining

    pnl_label = f"{net_pnl:+,.0f}" if abs(net_pnl) < 10000 else f"{net_pnl:+,.2f}"
    close_action = "COVER" if side == "short" else "SELL"
    print(f"  [{close_action}/{side_label}] {pos['name']}({code}) @ {price:,.2f} x {sell_shares:.4f} | PnL: {pnl_label} JPY ({reason})")

    append_trade_history({
        "timestamp": datetime.now().isoformat(),
        "symbol": code,
        "action": "CLOSE",
        "price": price,
        "shares": round(sell_shares, 6),
        "pnl": round(net_pnl, 2),
        "strategy": pos.get("strategy", ""),
        "side": side,
        "market": market,
        "name": pos.get("name", ""),
        "entry_price": pos.get("entry_price"),
        "reason": reason,
    })

    # 通知
    try:
        if side == "short":
            pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
        else:
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        from notifier import notify_sell_signal
        notify_sell_signal(code, pos["name"], price, net_pnl, pnl_pct, reason)
    except Exception:
        pass

    return portfolio


# ==============================================================
# メインスキャン＆トレードループ
# ==============================================================

def scan_and_trade(portfolio: dict, markets: list, dry_run: bool = False) -> dict:
    """全市場をスキャンし、条件に合う銘柄で売買する。

    ロング: テクニカルBUYシグナル + ファンダスコア >= 0.1 → 買いエントリー
    ショート: テクニカルSELLシグナル + ファンダスコア < 0 → 空売りエントリー
    """
    params = {}
    params_path = os.path.join(BASE_DIR, "optimized_params.json")
    if os.path.exists(params_path):
        with open(params_path, "r") as f:
            params = json.load(f)

    leverage = portfolio.get("leverage", DEFAULT_LEVERAGE)
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"  全市場統合ペーパートレード [{now.strftime('%Y-%m-%d %H:%M')}]")
    print(f"  資金: {portfolio['cash_jpy']:,.0f} JPY / ポジション: {len(portfolio['positions'])}件 / レバレッジ: {leverage}x")
    print(f"{'='*60}")

    # Step 1: 損切り・利確チェック
    print(f"\n[Step 1] 損切り・利確チェック...")
    actions = check_stop_loss_take_profit(portfolio)
    for action in actions:
        side_label = action.get("side", "long").upper()
        if dry_run:
            print(f"  [DRY] {action['action']} ({side_label}): {action['name']}({action['code']}) PnL: {action['pnl_pct']*100:+.1f}%")
        else:
            portfolio = execute_sell(
                portfolio, action["code"], action["current_price"],
                action["shares"], action["action"]
            )
            # 段階的利確の場合、tp_stageを更新（ポジションが残っている場合のみ）
            if "_tp_stage_after" in action:
                for p in portfolio["positions"]:
                    if p["code"] == action["code"]:
                        p["tp_stage"] = action["_tp_stage_after"]
                        break

    # トレーリングストップ用の高値/安値をJSON永続化するため保存
    save_portfolio(portfolio)

    if not actions:
        print("  損切り・利確対象なし")

    # Step 2: 各市場をスキャン（BUY候補 + SHORT候補を同時に収集）
    buy_candidates = []    # ロングエントリー候補
    short_candidates = []  # ショートエントリー候補

    for market in markets:
        if market not in MARKET_STRATEGIES:
            continue

        from market_hours import should_scan
        if not should_scan(market):
            print(f"\n[Step 2] {market.upper()}: 閉場中のためスキップ")
            continue

        config = MARKET_STRATEGIES[market]
        tickers = SCAN_TICKERS.get(market, [])
        strategy_params = params.get(config["param_key"], {})
        strategy = config["class"](params=strategy_params)

        print(f"\n[Step 2] スキャン: {market.upper()} ({len(tickers)}銘柄)...")

        for ticker in tickers:
            code = ticker["code"]
            name = ticker["name"]

            # 既にポジション保有中ならスキップ（同一銘柄ロング+ショート同時保有禁止）
            if any(p["code"] == code for p in portfolio["positions"]):
                continue

            data = fetch_data(code, period=config["period"])
            if data is None:
                continue

            # テクニカルシグナル
            try:
                signals = strategy.generate_signals(data)
                signal = int(signals.iloc[-1])
            except Exception:
                continue

            current_price = float(data["close"].iloc[-1].item())

            # BUYシグナル → ロング候補
            if signal == 1:
                fundamental = get_market_fundamental_score(code, market)
                if fundamental["score"] >= 0.1:  # 閾値0.1: 浮動小数点丸め誤差(-0.0等)を排除し、意味ある優位性を要求
                    buy_candidates.append({
                        "code": code,
                        "name": name,
                        "market": market,
                        "price": current_price,
                        "signal": signal,
                        "fundamental": fundamental,
                        "strategy": config["param_key"],
                    })
                    print(f"  LONG候補: {name}({code}) @ {current_price:,.2f} | ファンダ: {fundamental['score']:+.1f} ({fundamental['reason']})")
                else:
                    print(f"  ファンダNG(LONG): {name}({code}) | スコア: {fundamental['score']:+.2f} ({fundamental['reason']})")

            # SELLシグナル → ショート候補
            elif signal == -1:
                fundamental = get_market_fundamental_score(code, market)
                # ショート条件: テクニカルSELL + ファンダスコア < 0（業績が悪い銘柄を空売り）
                if fundamental["score"] < 0:
                    short_candidates.append({
                        "code": code,
                        "name": name,
                        "market": market,
                        "price": current_price,
                        "signal": signal,
                        "fundamental": fundamental,
                        "strategy": config["param_key"],
                    })
                    print(f"  SHORT候補: {name}({code}) @ {current_price:,.2f} | ファンダ: {fundamental['score']:+.1f} ({fundamental['reason']})")
                else:
                    print(f"  ファンダNG(SHORT): {name}({code}) | スコア: {fundamental['score']:+.1f} ({fundamental['reason']})")

            time.sleep(0.3)

    # Step 3: 保有銘柄のシグナルチェック（クローズ判断）
    print(f"\n[Step 3] 保有銘柄のクローズシグナルチェック...")
    for pos in list(portfolio["positions"]):
        market = pos["market"]
        side = pos.get("side", "long")
        if market not in MARKET_STRATEGIES:
            continue
        config = MARKET_STRATEGIES[market]
        strategy_params = params.get(config["param_key"], {})
        strategy = config["class"](params=strategy_params)

        data = fetch_data(pos["code"], period=config["period"])
        if data is None:
            continue

        try:
            signals = strategy.generate_signals(data)
            signal = int(signals.iloc[-1])
        except Exception:
            continue

        current_price = float(data["close"].iloc[-1].item())

        # ロング保有中にSELLシグナル → 売り決済
        if side == "long" and signal == -1:
            if dry_run:
                pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
                print(f"  [DRY] SELL/LONG: {pos['name']}({pos['code']}) PnL: {pnl_pct*100:+.1f}%")
            else:
                portfolio = execute_sell(
                    portfolio, pos["code"], current_price,
                    pos["shares"], "SELL_SIGNAL"
                )

        # ショート保有中にBUYシグナル → 買い戻し決済
        elif side == "short" and signal == 1:
            if dry_run:
                pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"]
                print(f"  [DRY] COVER/SHORT: {pos['name']}({pos['code']}) PnL: {pnl_pct*100:+.1f}%")
            else:
                portfolio = execute_sell(
                    portfolio, pos["code"], current_price,
                    pos["shares"], "BUY_SIGNAL_COVER"
                )

    # Step 4: ロングエントリー（スコア順にソートして上位から）
    if buy_candidates and not dry_run:
        print(f"\n[Step 4] ロングエントリー ({len(buy_candidates)}候補)...")
        buy_candidates.sort(key=lambda x: x["fundamental"]["score"], reverse=True)

        for candidate in buy_candidates:
            if len(portfolio["positions"]) >= MAX_POSITIONS:
                print(f"  全体ポジション上限({MAX_POSITIONS})に達しました")
                break
            market_count = sum(1 for p in portfolio["positions"] if p.get("market") == candidate["market"])
            if market_count >= MAX_POSITIONS_PER_MARKET:
                print(f"  {candidate['market'].upper()} 市場枠上限({MAX_POSITIONS_PER_MARKET})に達しました")
                continue
            portfolio = execute_buy(
                portfolio, candidate["code"], candidate["name"],
                candidate["market"], candidate["price"],
                candidate["strategy"], candidate["fundamental"]
            )
    elif buy_candidates and dry_run:
        print(f"\n[Step 4] DRY RUN - ロングエントリーはスキップ ({len(buy_candidates)}候補)")

    # Step 5: ショートエントリー（ファンダスコアの悪い順にソートして上位から）
    if short_candidates and not dry_run:
        print(f"\n[Step 5] ショートエントリー ({len(short_candidates)}候補)...")
        short_candidates.sort(key=lambda x: x["fundamental"]["score"])  # スコアが低い順

        for candidate in short_candidates:
            if len(portfolio["positions"]) >= MAX_POSITIONS:
                print(f"  全体ポジション上限({MAX_POSITIONS})に達しました")
                break
            market_count = sum(1 for p in portfolio["positions"] if p.get("market") == candidate["market"])
            if market_count >= MAX_POSITIONS_PER_MARKET:
                print(f"  {candidate['market'].upper()} 市場枠上限({MAX_POSITIONS_PER_MARKET})に達しました")
                continue
            portfolio = execute_short(
                portfolio, candidate["code"], candidate["name"],
                candidate["market"], candidate["price"],
                candidate["strategy"], candidate["fundamental"]
            )
    elif short_candidates and dry_run:
        print(f"\n[Step 5] DRY RUN - ショートエントリーはスキップ ({len(short_candidates)}候補)")

    return portfolio


# ==============================================================
# サマリー・レポート
# ==============================================================

def calc_portfolio_value(portfolio: dict) -> dict:
    """ポートフォリオの現在価値を計算する。

    ロングとショートで損益計算の方向が異なる:
    - ロング: 値上がり = 利益
    - ショート: 値下がり = 利益（参入価格 - 現在価格）
    """
    position_value = 0.0
    unrealized_pnl = 0.0
    position_details = []
    fx_rate = get_usdjpy_rate()

    for pos in portfolio["positions"]:
        market = pos.get("market", "jp")
        side = pos.get("side", "long")
        data = fetch_data(pos["code"], period="5d", min_rows=1)
        data_date = None
        if data is not None:
            current_price = float(data["close"].iloc[-1].item())
            data_date = str(data.index[-1].date()) if hasattr(data.index[-1], 'date') else str(data.index[-1])
        else:
            current_price = pos["entry_price"]

        # 円建てに変換（米国株・ゴールドはドル建て→円換算）
        if side == "short":
            # ショート: 損益 = (参入価格 - 現在価格) x 株数
            pnl_local = (pos["entry_price"] - current_price) * pos["shares"]
            pnl = price_to_jpy(pnl_local, market, fx_rate, code=pos["code"])
            pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"] * 100
            # ショートのポジション価値 = 証拠金 + 含み損益
            entry_value = price_to_jpy(pos["entry_price"], market, fx_rate, code=pos["code"]) * pos["shares"]
            value = entry_value + pnl
        else:
            # ロング: 普通に現在価格で評価
            value = price_to_jpy(current_price, market, fx_rate, code=pos["code"]) * pos["shares"]
            pnl = price_to_jpy(current_price - pos["entry_price"], market, fx_rate, code=pos["code"]) * pos["shares"]
            pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

        position_value += value
        unrealized_pnl += pnl
        position_details.append({
            **pos,
            "current_price": current_price,
            "data_date": data_date,
            "value_jpy": round(value, 2),
            "pnl_jpy": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    total_value = portfolio["cash_jpy"] + position_value
    total_return = (total_value / portfolio["initial_capital_jpy"] - 1) * 100

    # ロング/ショート別のポジション数をカウント
    long_count = sum(1 for p in portfolio["positions"] if p.get("side", "long") == "long")
    short_count = sum(1 for p in portfolio["positions"] if p.get("side", "long") == "short")

    return {
        "cash_jpy": portfolio["cash_jpy"],
        "position_value_jpy": position_value,
        "unrealized_pnl_jpy": unrealized_pnl,
        "total_value_jpy": total_value,
        "total_return_pct": total_return,
        "realized_pnl_jpy": portfolio["total_realized_pnl"],
        "total_trades": portfolio["total_trades"],
        "winning_trades": portfolio["winning_trades"],
        "losing_trades": portfolio["losing_trades"],
        "win_rate": (portfolio["winning_trades"] / portfolio["total_trades"] * 100)
                    if portfolio["total_trades"] > 0 else 0.0,
        "positions": position_details,
        "position_count": len(portfolio["positions"]),
        "long_count": long_count,
        "short_count": short_count,
        "leverage": portfolio.get("leverage", DEFAULT_LEVERAGE),
    }


def print_summary(portfolio: dict):
    """ポートフォリオサマリーを表示する。"""
    val = calc_portfolio_value(portfolio)
    leverage = val.get("leverage", DEFAULT_LEVERAGE)

    print(f"\n{'='*65}")
    print(f"  UNIFIED PAPER TRADE PORTFOLIO")
    print(f"  開始: {portfolio['created_at'][:10]} | 更新: {portfolio['last_updated'][:16]} | レバレッジ: {leverage}x")
    print(f"{'='*65}")

    pnl_sign = "+" if val["total_return_pct"] >= 0 else ""
    print(f"  初期資金:     {portfolio['initial_capital_jpy']:>10,.0f} JPY ($200)")
    print(f"  現金:         {val['cash_jpy']:>10,.0f} JPY")
    print(f"  購買力:       {val['cash_jpy'] * leverage:>10,.0f} JPY ({leverage}x)")
    print(f"  ポジション価値: {val['position_value_jpy']:>10,.0f} JPY")
    print(f"  含み損益:     {val['unrealized_pnl_jpy']:>+10,.0f} JPY")
    print(f"  確定損益:     {val['realized_pnl_jpy']:>+10,.0f} JPY")
    print(f"  ───────────────────────────────")
    print(f"  総資産:       {val['total_value_jpy']:>10,.0f} JPY ({pnl_sign}{val['total_return_pct']:.1f}%)")
    print()
    print(f"  取引回数: {val['total_trades']} (勝: {val['winning_trades']} / 負: {val['losing_trades']})")
    print(f"  勝率: {val['win_rate']:.1f}%")
    print(f"  ポジション: {val['position_count']}/{MAX_POSITIONS} (LONG: {val['long_count']} / SHORT: {val['short_count']})")
    print()

    # ポジション一覧（LONG/SHORT表示付き）
    if val["positions"]:
        print(f"  {'銘柄':<18} {'L/S':>5} {'市場':>4} {'参入値':>10} {'現在値':>10} {'損益':>10} {'損益%':>8}")
        print(f"  {'-'*18} {'-'*5} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
        for p in val["positions"]:
            side_label = p.get("side", "long").upper()
            print(f"  {p['name'][:16]:<18} {side_label:>5} {p['market']:>4} {p['entry_price']:>10,.2f} "
                  f"{p['current_price']:>10,.2f} {p['pnl_jpy']:>+10,.0f} {p['pnl_pct']:>+7.1f}%")
    else:
        print("  ポジションなし")

    # 決済済みトレード（直近5件）
    if portfolio["closed_trades"]:
        print(f"\n  直近の決済トレード:")
        for t in portfolio["closed_trades"][-5:]:
            side_label = t.get("side", "long").upper()
            print(f"    {t['exit_date'][:10]} [{side_label}] {t['name'][:12]}({t['code']}) "
                  f"PnL: {t['net_pnl_jpy']:+,.0f} JPY ({t['reason']})")

    print(f"{'='*65}\n")


def generate_daily_report(portfolio: dict):
    """日次レポートをMarkdownで生成する。"""
    val = calc_portfolio_value(portfolio)
    now = datetime.now()
    deadline = datetime(2026, 3, 30)
    remaining = (deadline - now).days

    lines = []
    lines.append(f"# 日次レポート {now.strftime('%Y-%m-%d')}\n")
    lines.append(f"残り{remaining}日（期限: 2026/03/30）\n")
    lines.append(f"## ポートフォリオ状況\n")
    lines.append(f"| 項目 | 金額 |")
    lines.append(f"|------|------|")
    lines.append(f"| 初期資金 | {portfolio['initial_capital_jpy']:,.0f} JPY |")
    lines.append(f"| 総資産 | {val['total_value_jpy']:,.0f} JPY |")
    lines.append(f"| 収益率 | {val['total_return_pct']:+.1f}% |")
    lines.append(f"| 確定損益 | {val['realized_pnl_jpy']:+,.0f} JPY |")
    lines.append(f"| 含み損益 | {val['unrealized_pnl_jpy']:+,.0f} JPY |")
    lines.append(f"| 取引回数 | {val['total_trades']}回 (勝率: {val['win_rate']:.0f}%) |")
    lines.append(f"| ポジション数 | {val['position_count']}/{MAX_POSITIONS} (L:{val['long_count']} / S:{val['short_count']}) |")
    lines.append(f"| レバレッジ | {val.get('leverage', DEFAULT_LEVERAGE)}x |")
    lines.append("")

    if val["positions"]:
        lines.append(f"## 保有ポジション\n")
        lines.append(f"| 銘柄 | L/S | 市場 | 参入値 | 現在値 | 損益% | データ日付 |")
        lines.append(f"|------|-----|------|--------|--------|-------|----------|")
        for p in val["positions"]:
            dd = p.get('data_date', '?')
            side_label = p.get("side", "long").upper()
            note = ""
            if dd and p['entry_date'][:10] <= dd and abs(p['pnl_pct']) < 0.01:
                note = " (市場閉場中)"
            lines.append(f"| {p['name']}({p['code']}) | {side_label} | {p['market']} | "
                        f"{p['entry_price']:,.2f} | {p['current_price']:,.2f} | "
                        f"{p['pnl_pct']:+.1f}%{note} | {dd} |")
        lines.append("")

    if portfolio["closed_trades"]:
        today_trades = [t for t in portfolio["closed_trades"]
                       if t["exit_date"][:10] == now.strftime("%Y-%m-%d")]
        if today_trades:
            lines.append(f"## 本日の取引\n")
            for t in today_trades:
                lines.append(f"- {t['name']}({t['code']}): {t['net_pnl_jpy']:+,.0f} JPY ({t['reason']})")
            lines.append("")

    report = "\n".join(lines)

    with open(DAILY_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  日次レポート生成: {DAILY_REPORT_FILE}")
    print(report)


# ==============================================================
# ポートフォリオログ（資産推移記録）
# ==============================================================

def record_portfolio_snapshot(portfolio: dict):
    """ポートフォリオのスナップショットをログに追記する。"""
    val = calc_portfolio_value(portfolio)
    log = load_portfolio_log()
    log.append({
        "timestamp": datetime.now().isoformat(),
        "total_value_jpy": round(val["total_value_jpy"], 2),
        "cash_jpy": round(val["cash_jpy"], 2),
        "position_value_jpy": round(val["position_value_jpy"], 2),
        "unrealized_pnl_jpy": round(val["unrealized_pnl_jpy"], 2),
        "realized_pnl_jpy": round(val["realized_pnl_jpy"], 2),
        "total_return_pct": round(val["total_return_pct"], 2),
        "position_count": val["position_count"],
        "total_trades": val["total_trades"],
    })
    save_portfolio_log(log)


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="全市場統合ペーパートレーダー ($200, 期限: 2026/03/30)"
    )
    parser.add_argument(
        "--market", choices=["jp", "us", "btc", "gold", "fx"],
        help="特定の市場のみ巡回"
    )
    parser.add_argument("--summary", action="store_true", help="ポートフォリオ状況表示")
    parser.add_argument("--report", action="store_true", help="日次レポート生成")
    parser.add_argument("--reset", action="store_true", help="ポートフォリオリセット")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="シグナル確認のみ（売買しない）")
    parser.add_argument("--json", action="store_true", help="JSON出力")
    parser.add_argument("--monitor", action="store_true",
                        help="保有ポジションの損切り/利確チェックのみ（新規スキャンなし）")
    args = parser.parse_args()

    # リセット
    if args.reset:
        portfolio = create_initial_portfolio()
        save_portfolio(portfolio)
        save_portfolio_log([])
        print("  ポートフォリオをリセットしました。")
        print(f"  初期資金: {INITIAL_CAPITAL_JPY:,.0f} JPY ($200)")
        return

    portfolio = load_portfolio()

    # サマリー
    if args.summary:
        if args.json:
            val = calc_portfolio_value(portfolio)
            print(json.dumps(val, ensure_ascii=False, indent=2, default=str))
        else:
            print_summary(portfolio)
        return

    # レポート
    if args.report:
        generate_daily_report(portfolio)
        return

    # メイン: スキャン＆トレード
    # モニターモード: 損切り/利確チェックのみ
    if args.monitor:
        print(f"[MONITOR] 保有ポジションのSL/TPチェック...")
        actions = check_stop_loss_take_profit(portfolio)
        for action in actions:
            portfolio = execute_sell(
                portfolio, action["code"], action["current_price"],
                action["shares"], action["action"]
            )
            if "_tp_stage_after" in action:
                for p in portfolio["positions"]:
                    if p["code"] == action["code"]:
                        p["tp_stage"] = action["_tp_stage_after"]
                        break
        if not actions:
            print("  損切り・利確対象なし")
        save_portfolio(portfolio)
        record_portfolio_snapshot(portfolio)
        print_summary(portfolio)
        return

    markets = [args.market] if args.market else ["jp", "us", "btc", "gold", "fx"]
    portfolio = scan_and_trade(portfolio, markets, dry_run=args.dry_run)

    if not args.dry_run:
        save_portfolio(portfolio)
        record_portfolio_snapshot(portfolio)

    print_summary(portfolio)

    # 日次レポートも自動生成
    generate_daily_report(portfolio)

    # Discord日次通知
    try:
        val = calc_portfolio_value(portfolio)
        from notifier import notify_portfolio_update
        notify_portfolio_update(
            val["total_value_jpy"], val["total_return_pct"],
            val["position_count"], val["cash_jpy"]
        )
    except Exception:
        pass

    # 期限チェック
    deadline = datetime(2026, 3, 30)
    remaining = (deadline - datetime.now()).days
    if remaining <= 0:
        print("  [DEADLINE] 3/30の期限に到達しました。結果を確認してください。")
    elif remaining <= 7:
        print(f"  [WARNING] 期限まで残り{remaining}日。ポジションの整理を検討してください。")


if __name__ == "__main__":
    main()

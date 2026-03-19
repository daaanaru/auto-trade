"""
live_trade.py — 取引所API接続レイヤー（実弾投入用）

ccxt経由でbitFlyer/GMOコインに実際の売買注文を出す。
paper_trade.py と同じインターフェースで、切り替え可能な設計。

安全機能:
  - DRY_RUN モード（デフォルトON）: 注文内容をログに出すだけで実際に発注しない
  - 1回あたり最大金額リミット（デフォルト10,000円）
  - 月次損失リミット（-10%で自動停止）
  - 全ポジション強制決済コマンド

使い方:
  # DRY_RUNモード（デフォルト）: 注文をシミュレートするだけ
  python live_trade.py

  # 残高・ポジション確認
  python live_trade.py --status

  # 実弾モード（.envにLIVE_TRADE_DRY_RUN=falseが必要）
  python live_trade.py --execute

  # 全ポジション強制決済
  python live_trade.py --close-all

  # 戦略を指定
  python live_trade.py --strategy bb_rsi

  # 状態リセット
  python live_trade.py --reset
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import ccxt
except ImportError:
    print("ERROR: ccxt がインストールされていません。 pip install ccxt を実行してください。")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from trade_engine import TradeEngine
from engine import CCXTFetcher
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy

# ==============================================================
# 設定
# ==============================================================

STATE_FILE = PROJECT_ROOT / "live_trade_state.json"
TRADE_LOG_FILE = PROJECT_ROOT / "live_trade_log.json"
OPTIMIZED_PARAMS_FILE = PROJECT_ROOT / "optimized_params.json"

# 戦略マッピング（paper_trade.pyと同一）
STRATEGY_MAP = {
    "vol_div": {
        "class": VolumeDivergenceStrategy,
        "param_key": "vol_div",
        "name": "Volume Divergence",
    },
    "bb_rsi": {
        "class": BBRSIComboStrategy,
        "param_key": "bb_rsi",
        "name": "BB+RSI Combo",
    },
    "sma": {
        "class": SMACrossoverStrategy,
        "param_key": "sma",
        "name": "SMA Crossover",
    },
    "mom_pb": {
        "class": MomentumPullbackStrategy,
        "param_key": "mom_pb",
        "name": "Momentum Pullback",
    },
}


# ==============================================================
# 取引所接続
# ==============================================================

class ExchangeClient:
    """ccxt経由の取引所クライアント。DRY_RUNモード対応。"""

    def __init__(self):
        self.exchange_id = os.getenv("LIVE_TRADE_EXCHANGE", "bitflyer")
        self.dry_run = os.getenv("LIVE_TRADE_DRY_RUN", "true").lower() != "false"
        self.max_order_jpy = int(os.getenv("LIVE_TRADE_MAX_ORDER_JPY", "10000"))
        self.monthly_loss_limit_pct = float(os.getenv("LIVE_TRADE_MONTHLY_LOSS_LIMIT_PCT", "10.0"))
        self.symbol = os.getenv("LIVE_TRADE_SYMBOL", "BTC/JPY")

        api_key = os.getenv("LIVE_TRADE_API_KEY", "")
        api_secret = os.getenv("LIVE_TRADE_API_SECRET", "")

        if not api_key or not api_secret:
            if not self.dry_run:
                print("ERROR: LIVE_TRADE_API_KEY と LIVE_TRADE_API_SECRET が .env に設定されていません。")
                print("       DRY_RUNモードで動作します。")
                self.dry_run = True

        # ccxtで利用可能な取引所を動的に検出
        supported = {}
        if hasattr(ccxt, "bitflyer"):
            supported["bitflyer"] = ccxt.bitflyer
        # GMOコインはccxtバージョンにより利用不可の場合がある
        for name in ("gmo", "gmocoin"):
            if hasattr(ccxt, name):
                supported["gmocoin"] = getattr(ccxt, name)
                break

        if self.exchange_id not in supported:
            fallback = list(supported.keys())[0] if supported else None
            if fallback:
                print(f"WARNING: '{self.exchange_id}' は未対応。{fallback}を使用します。")
                self.exchange_id = fallback
            else:
                print(f"ERROR: 利用可能な取引所がccxtに見つかりません。")
                if not self.dry_run:
                    sys.exit(1)
                # DRY_RUNならダミーで続行
                self.exchange = None
                return

        config = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        }
        self.exchange = supported[self.exchange_id](config)

    def get_ticker(self) -> dict:
        """現在価格を取得する。"""
        if self.exchange is None:
            return {"last": 0, "bid": 0, "ask": 0, "symbol": self.symbol,
                    "note": "DRY_RUNモード: 取引所未接続"}
        ticker = self.exchange.fetch_ticker(self.symbol)
        return {
            "last": ticker["last"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "symbol": self.symbol,
        }

    def get_balance(self) -> dict:
        """口座残高を取得する。"""
        if self.dry_run:
            return {"JPY": {"free": 0, "used": 0, "total": 0},
                    "BTC": {"free": 0, "used": 0, "total": 0},
                    "note": "DRY_RUNモード: 実際の残高は取得しません"}

        balance = self.exchange.fetch_balance()
        return {
            "JPY": {
                "free": balance.get("JPY", {}).get("free", 0),
                "used": balance.get("JPY", {}).get("used", 0),
                "total": balance.get("JPY", {}).get("total", 0),
            },
            "BTC": {
                "free": balance.get("BTC", {}).get("free", 0),
                "used": balance.get("BTC", {}).get("used", 0),
                "total": balance.get("BTC", {}).get("total", 0),
            },
        }

    def place_market_order(self, side: str, amount: float, price: float, is_close: bool = False) -> dict:
        """成行注文を出す。

        Args:
            side: "buy" or "sell"
            amount: BTC数量
            price: 参考価格（金額チェック用）
            is_close: True=決済注文（金額上限チェックをスキップ）

        Returns:
            注文結果の辞書
        """
        order_jpy = price * amount
        if not is_close and order_jpy > self.max_order_jpy:
            return {
                "status": "rejected",
                "reason": f"注文金額 ¥{order_jpy:,.0f} が上限 ¥{self.max_order_jpy:,} を超えています",
                "order_jpy": order_jpy,
                "max_jpy": self.max_order_jpy,
            }

        if self.dry_run:
            return {
                "status": "dry_run",
                "side": side,
                "amount": amount,
                "price": price,
                "order_jpy": order_jpy,
                "symbol": self.symbol,
                "timestamp": datetime.now().isoformat(),
                "message": f"[DRY_RUN] {side.upper()} {amount:.8f} BTC @ ¥{price:,.0f} (≈¥{order_jpy:,.0f})",
            }

        order = self.exchange.create_market_order(
            symbol=self.symbol,
            side=side,
            amount=amount,
        )
        return {
            "status": "executed",
            "order_id": order.get("id"),
            "side": side,
            "amount": amount,
            "price": order.get("average", price),
            "order_jpy": order.get("cost", order_jpy),
            "symbol": self.symbol,
            "timestamp": datetime.now().isoformat(),
        }

    def place_limit_order(self, side: str, amount: float, price: float) -> dict:
        """指値注文を出す。

        Args:
            side: "buy" or "sell"
            amount: BTC数量
            price: 指値価格
        """
        order_jpy = price * amount
        if order_jpy > self.max_order_jpy:
            return {
                "status": "rejected",
                "reason": f"注文金額 ¥{order_jpy:,.0f} が上限 ¥{self.max_order_jpy:,} を超えています",
            }

        if self.dry_run:
            return {
                "status": "dry_run",
                "side": side,
                "amount": amount,
                "price": price,
                "order_jpy": order_jpy,
                "symbol": self.symbol,
                "timestamp": datetime.now().isoformat(),
                "message": f"[DRY_RUN] LIMIT {side.upper()} {amount:.8f} BTC @ ¥{price:,.0f}",
            }

        order = self.exchange.create_limit_order(
            symbol=self.symbol,
            side=side,
            amount=amount,
            price=price,
        )
        return {
            "status": "executed",
            "order_id": order.get("id"),
            "side": side,
            "amount": amount,
            "price": price,
            "symbol": self.symbol,
            "timestamp": datetime.now().isoformat(),
        }


# ==============================================================
# 状態管理（paper_trade.pyと同じ構造）
# ==============================================================

DEFAULT_CONFIG = {
    # シグナル取得と発注を同じ取引所・同じペアで統一
    # ExchangeClient側のLIVE_TRADE_SYMBOL/LIVE_TRADE_EXCHANGEと一致させること
    "symbol": os.getenv("LIVE_TRADE_SYMBOL", "BTC/JPY"),
    "exchange": os.getenv("LIVE_TRADE_EXCHANGE", "bitflyer"),
    "interval": "1d",
    "lookback_days": 120,
    "initial_capital_jpy": 10000,  # 初期投入額（円）
    "commission_rate": 0.001,
    "slippage_rate": 0.0005,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # 環境変数とJSONのconfig不整合を自動修正（旧BTC/USDT→新BTC/JPY等）
        current_symbol = DEFAULT_CONFIG["symbol"]
        current_exchange = DEFAULT_CONFIG["exchange"]
        if state.get("config", {}).get("symbol") != current_symbol or \
           state.get("config", {}).get("exchange") != current_exchange:
            old_sym = state.get("config", {}).get("symbol", "?")
            old_ex = state.get("config", {}).get("exchange", "?")
            print(f"  [MIGRATE] config更新: {old_ex}/{old_sym} → {current_exchange}/{current_symbol}")
            state["config"]["symbol"] = current_symbol
            state["config"]["exchange"] = current_exchange
        return state
    return create_initial_state()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def create_initial_state() -> dict:
    return {
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "config": DEFAULT_CONFIG,
        "capital_jpy": DEFAULT_CONFIG["initial_capital_jpy"],
        "position": 0.0,
        "entry_price": 0.0,
        "total_pnl_jpy": 0.0,
        "monthly_pnl_jpy": 0.0,
        "monthly_reset_date": datetime.now().strftime("%Y-%m-01"),
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "current_signal": 0,
        "strategy": "vol_div",
        "is_halted": False,
        "halt_reason": "",
    }


def load_trade_log() -> list:
    if TRADE_LOG_FILE.exists():
        with open(TRADE_LOG_FILE, "r") as f:
            return json.load(f)
    return []


def save_trade_log(log: list):
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# 月次損失チェック
# ==============================================================

def check_monthly_loss(state: dict, client: ExchangeClient) -> bool:
    """月次損失が限度を超えていないかチェック。超えていたらTrue。"""
    current_month = datetime.now().strftime("%Y-%m-01")
    if state.get("monthly_reset_date") != current_month:
        state["monthly_pnl_jpy"] = 0.0
        state["monthly_reset_date"] = current_month
        state["is_halted"] = False
        state["halt_reason"] = ""

    if state["is_halted"]:
        return True

    initial = state["config"]["initial_capital_jpy"]
    if initial == 0:
        return False
    loss_pct = abs(state["monthly_pnl_jpy"]) / initial * 100

    if state["monthly_pnl_jpy"] < 0 and loss_pct >= client.monthly_loss_limit_pct:
        state["is_halted"] = True
        state["halt_reason"] = f"月次損失 ¥{state['monthly_pnl_jpy']:,.0f} ({loss_pct:.1f}%) が上限 {client.monthly_loss_limit_pct}% を超過"
        save_state(state)
        return True

    return False


# ==============================================================
# 戦略実行
# ==============================================================

def get_strategy(strategy_key: str):
    if strategy_key not in STRATEGY_MAP:
        raise ValueError(f"Unknown strategy: {strategy_key}. Available: {list(STRATEGY_MAP.keys())}")
    info = STRATEGY_MAP[strategy_key]
    params = {}
    if OPTIMIZED_PARAMS_FILE.exists():
        with open(OPTIMIZED_PARAMS_FILE, "r") as f:
            all_params = json.load(f)
            params = all_params.get(info["param_key"], {})
    return info["class"](params=params), info["name"]


def evaluate_signal(data: pd.DataFrame, strategy) -> int:
    signals = strategy.generate_signals(data)
    return int(signals.iloc[-1])


# ==============================================================
# 取引実行
# ==============================================================

def execute_live_trade(state: dict, signal: int, client: ExchangeClient) -> dict:
    """シグナルに基づいて実際の売買を実行する。"""
    trade_log = load_trade_log()

    # 月次損失チェック
    if check_monthly_loss(state, client):
        print(f"  [HALTED] {state['halt_reason']}")
        print("  月次損失リミットに達しました。今月の取引を停止します。")
        return state

    # 現在価格を取得
    ticker = client.get_ticker()
    current_price = ticker["last"]
    print(f"  Current Price: ¥{current_price:,.0f}")

    trade_executed = False

    # --- ポジションクローズ判定 ---
    if state["position"] != 0:
        should_close = False
        if state["position"] > 0 and signal == -1:
            should_close = True
        elif state["position"] < 0 and signal == 1:
            should_close = True

        if should_close:
            entry_price = state["entry_price"]
            position_size = abs(state["position"])
            direction = 1 if state["position"] > 0 else -1

            # 決済注文（金額上限チェックをスキップ）
            close_side = "sell" if direction == 1 else "buy"
            result = client.place_market_order(close_side, position_size, current_price, is_close=True)

            if result["status"] == "rejected":
                print(f"  [REJECTED] {result['reason']}")
                return state

            actual_price = result.get("price", current_price)
            pnl_jpy = direction * (actual_price - entry_price) * position_size

            state["capital_jpy"] += pnl_jpy
            state["total_pnl_jpy"] += pnl_jpy
            state["monthly_pnl_jpy"] += pnl_jpy
            state["total_trades"] += 1
            if pnl_jpy > 0:
                state["winning_trades"] += 1
            else:
                state["losing_trades"] += 1

            status_label = "DRY_RUN" if result["status"] == "dry_run" else "LIVE"
            print(f"  [{status_label}][CLOSE] {'LONG' if direction == 1 else 'SHORT'} @ ¥{actual_price:,.0f}")
            print(f"          PnL: ¥{pnl_jpy:+,.0f} (entry: ¥{entry_price:,.0f})")

            trade_record = {
                "timestamp": datetime.now().isoformat(),
                "action": "CLOSE",
                "mode": result["status"],
                "direction": "LONG" if direction == 1 else "SHORT",
                "entry_price": entry_price,
                "exit_price": actual_price,
                "size": position_size,
                "pnl_jpy": round(pnl_jpy),
                "capital_after_jpy": round(state["capital_jpy"]),
                "order_result": result,
            }
            trade_log.append(trade_record)

            state["position"] = 0.0
            state["entry_price"] = 0.0
            trade_executed = True

    # --- エントリー判定 ---
    if signal != 0 and state["position"] == 0:
        # スポット口座ではショート（空売り）禁止
        # margin/futures口座を設定するまでロングのみ許可
        if signal == -1:
            print("  [BLOCKED] スポット口座でのショートエントリーは禁止されています")
            return state

        risk_capital = state["capital_jpy"] * 0.05
        position_size = risk_capital / current_price

        entry_side = "buy" if signal == 1 else "sell"
        result = client.place_market_order(entry_side, position_size, current_price)

        if result["status"] == "rejected":
            print(f"  [REJECTED] {result['reason']}")
            return state

        actual_price = result.get("price", current_price)

        if signal == 1:
            state["position"] = position_size
        else:
            state["position"] = -position_size
        state["entry_price"] = actual_price

        status_label = "DRY_RUN" if result["status"] == "dry_run" else "LIVE"
        action = "BUY (LONG)" if signal == 1 else "SELL (SHORT)"
        print(f"  [{status_label}][ENTRY] {action} @ ¥{actual_price:,.0f} x {position_size:.8f} BTC")
        print(f"          Order: ≈¥{actual_price * position_size:,.0f}")

        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "action": "ENTRY",
            "mode": result["status"],
            "direction": "LONG" if signal == 1 else "SHORT",
            "price": actual_price,
            "size": position_size,
            "capital_after_jpy": round(state["capital_jpy"]),
            "order_result": result,
        }
        trade_log.append(trade_record)
        trade_executed = True

    if not trade_executed:
        print(f"  [HOLD] signal={signal}, position={state['position']:.8f} BTC")

    state["current_signal"] = signal
    state["last_updated"] = datetime.now().isoformat()

    save_trade_log(trade_log)
    return state


# ==============================================================
# 全ポジション強制決済
# ==============================================================

def close_all_positions(state: dict, client: ExchangeClient) -> dict:
    """全ポジションを成行で強制決済する。"""
    if state["position"] == 0:
        print("  ポジションなし。決済不要です。")
        return state

    ticker = client.get_ticker()
    current_price = ticker["last"]
    position_size = abs(state["position"])
    direction = 1 if state["position"] > 0 else -1
    close_side = "sell" if direction == 1 else "buy"

    print(f"  [EMERGENCY CLOSE] {'LONG' if direction == 1 else 'SHORT'} {position_size:.8f} BTC @ ¥{current_price:,.0f}")

    result = client.place_market_order(close_side, position_size, current_price, is_close=True)

    if result["status"] == "rejected":
        print(f"  [REJECTED] {result['reason']}")
        print("  手動で取引所にログインして決済してください。")
        return state

    actual_price = result.get("price", current_price)
    pnl_jpy = direction * (actual_price - state["entry_price"]) * position_size

    state["capital_jpy"] += pnl_jpy
    state["total_pnl_jpy"] += pnl_jpy
    state["monthly_pnl_jpy"] += pnl_jpy
    state["total_trades"] += 1
    if pnl_jpy > 0:
        state["winning_trades"] += 1
    else:
        state["losing_trades"] += 1

    state["position"] = 0.0
    state["entry_price"] = 0.0

    trade_log = load_trade_log()
    trade_log.append({
        "timestamp": datetime.now().isoformat(),
        "action": "EMERGENCY_CLOSE",
        "mode": result["status"],
        "direction": "LONG" if direction == 1 else "SHORT",
        "exit_price": actual_price,
        "size": position_size,
        "pnl_jpy": round(pnl_jpy),
        "order_result": result,
    })
    save_trade_log(trade_log)

    status_label = "DRY_RUN" if result["status"] == "dry_run" else "LIVE"
    print(f"  [{status_label}] 決済完了。PnL: ¥{pnl_jpy:+,.0f}")

    state["last_updated"] = datetime.now().isoformat()
    save_state(state)
    return state


# ==============================================================
# ステータス表示
# ==============================================================

def print_status(state: dict, client: ExchangeClient):
    """口座状況とトレード統計を表示する。"""
    print("\n" + "=" * 60)
    print("  LIVE TRADE STATUS")
    print("=" * 60)

    # モード表示
    mode = "DRY_RUN (注文はシミュレートのみ)" if client.dry_run else "LIVE (実際に発注します)"
    print(f"  Mode          : {mode}")
    print(f"  Exchange      : {client.exchange_id}")
    print(f"  Symbol        : {client.symbol}")
    print(f"  Max Order     : ¥{client.max_order_jpy:,}")
    print(f"  Monthly Limit : -{client.monthly_loss_limit_pct}%")

    strategy_name = STRATEGY_MAP.get(state.get("strategy", "vol_div"), {}).get("name", "Unknown")
    print(f"  Strategy      : {strategy_name}")
    print()

    # 停止状態
    if state.get("is_halted"):
        print(f"  *** HALTED: {state['halt_reason']} ***")
        print()

    # 資産状況
    initial = state["config"]["initial_capital_jpy"]
    capital = state["capital_jpy"]
    print(f"  Initial Capital : ¥{initial:,}")
    print(f"  Current Capital : ¥{capital:,.0f}")
    print(f"  Total PnL       : ¥{state['total_pnl_jpy']:+,.0f}")
    print(f"  Monthly PnL     : ¥{state['monthly_pnl_jpy']:+,.0f}")
    print()

    # 取引所残高（LIVE時のみ）
    if not client.dry_run:
        try:
            balance = client.get_balance()
            print(f"  Exchange JPY  : ¥{balance['JPY']['total']:,.0f} (free: ¥{balance['JPY']['free']:,.0f})")
            print(f"  Exchange BTC  : {balance['BTC']['total']:.8f} (free: {balance['BTC']['free']:.8f})")
            print()
        except Exception as e:
            print(f"  Exchange balance: ERROR ({e})")
            print()

    # ポジション
    if state["position"] > 0:
        pos_str = f"LONG {abs(state['position']):.8f} BTC @ ¥{state['entry_price']:,.0f}"
    elif state["position"] < 0:
        pos_str = f"SHORT {abs(state['position']):.8f} BTC @ ¥{state['entry_price']:,.0f}"
    else:
        pos_str = "NO POSITION"

    print(f"  Position      : {pos_str}")
    print(f"  Last Signal   : {state['current_signal']}")

    # 現在価格
    try:
        ticker = client.get_ticker()
        print(f"  Current Price : ¥{ticker['last']:,.0f}")
        if state["position"] != 0:
            direction = 1 if state["position"] > 0 else -1
            unrealized = direction * (ticker["last"] - state["entry_price"]) * abs(state["position"])
            print(f"  Unrealized PnL: ¥{unrealized:+,.0f}")
    except Exception:
        pass

    # トレード統計
    print()
    total = state["total_trades"]
    win = state["winning_trades"]
    lose = state["losing_trades"]
    win_rate = (win / total * 100) if total > 0 else 0
    print(f"  Total Trades  : {total}")
    print(f"  Wins / Losses : {win} / {lose}")
    print(f"  Win Rate      : {win_rate:.1f}%")

    print("=" * 60)

    # 最近のログ
    trade_log = load_trade_log()
    if trade_log:
        print("\n  Recent Trades (last 5):")
        print("  " + "-" * 56)
        for t in trade_log[-5:]:
            ts = t["timestamp"][:16]
            mode_tag = f"[{t.get('mode', '?')}]"
            if t["action"] in ("CLOSE", "EMERGENCY_CLOSE"):
                pnl = t.get("pnl_jpy", 0)
                print(f"  {ts} {mode_tag} {t['action']:15} {t['direction']:5} @ ¥{t['exit_price']:>12,.0f} PnL: ¥{pnl:+,.0f}")
            else:
                print(f"  {ts} {mode_tag} {t['action']:15} {t['direction']:5} @ ¥{t['price']:>12,.0f} x {t['size']:.8f}")
        print()


# ==============================================================
# TradeEngine 準拠クラス
# ==============================================================

class LiveTradeEngine(TradeEngine):
    """実弾トレードエンジン。TradeEngine基底クラスを継承。

    ccxt経由でbitFlyerに実際の売買注文を出す。
    PaperTradeEngineと同じインターフェースで切り替え可能。
    """

    def __init__(self, strategy_key: str = "vol_div"):
        super().__init__(strategy_key)
        self.client = ExchangeClient()
        self.state = load_state()
        if self.state.get("strategy") != strategy_key:
            self.state["strategy"] = strategy_key
            save_state(self.state)

    def run(self) -> dict:
        if self.state.get("is_halted"):
            print(f"  *** HALTED: {self.state['halt_reason']} ***")
            return self.state
        config = self.state["config"]
        fetcher = CCXTFetcher(exchange=config["exchange"])
        data = fetcher.fetch(
            symbol=config["symbol"],
            period=f"{config['lookback_days']}d",
            interval=config["interval"],
        )
        strategy, _ = get_strategy(self.strategy_key)
        signal = evaluate_signal(data, strategy)
        self.state = execute_live_trade(self.state, signal, self.client)
        save_state(self.state)
        return self.state

    def get_position(self) -> dict:
        pos = self.state["position"]
        direction = "LONG" if pos > 0 else ("SHORT" if pos < 0 else "NONE")
        return {
            "position": pos,
            "entry_price": self.state["entry_price"],
            "direction": direction,
        }

    def get_balance(self) -> dict:
        balance = self.client.get_balance()
        balance["capital_jpy"] = self.state["capital_jpy"]
        balance["total_pnl_jpy"] = self.state["total_pnl_jpy"]
        balance["monthly_pnl_jpy"] = self.state["monthly_pnl_jpy"]
        return balance

    def market_buy(self, amount: float) -> dict:
        ticker = self.client.get_ticker()
        return self.client.place_market_order("buy", amount, ticker["last"])

    def market_sell(self, amount: float) -> dict:
        ticker = self.client.get_ticker()
        return self.client.place_market_order("sell", amount, ticker["last"])

    def emergency_close(self) -> dict:
        self.state = close_all_positions(self.state, self.client)
        return self.state

    def status(self):
        print_status(self.state, self.client)

    def reset(self):
        self.state = create_initial_state()
        self.state["strategy"] = self.strategy_key
        save_state(self.state)
        save_trade_log([])


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(description="Live Trading System (bitFlyer / GMO via ccxt)")
    parser.add_argument("--reset", action="store_true", help="状態をリセットして初期化")
    parser.add_argument("--status", action="store_true", help="口座状況を表示（取引なし）")
    parser.add_argument("--close-all", action="store_true", dest="close_all", help="全ポジションを強制決済")
    parser.add_argument("--execute", action="store_true", help="実弾モードを強制ON（.envの設定より優先）")
    parser.add_argument("--strategy", type=str, default=None,
                        help=f"戦略を指定: {list(STRATEGY_MAP.keys())}")
    args = parser.parse_args()

    print("\n[Live Trade] Starting...")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 取引所クライアント初期化
    if args.execute:
        os.environ["LIVE_TRADE_DRY_RUN"] = "false"
    client = ExchangeClient()

    mode_label = "DRY_RUN" if client.dry_run else "*** LIVE ***"
    print(f"  Mode: {mode_label}")
    print(f"  Exchange: {client.exchange_id}")

    # リセット
    if args.reset:
        state = create_initial_state()
        if args.strategy:
            state["strategy"] = args.strategy
        save_state(state)
        save_trade_log([])
        print("  State reset to initial values.")
        print_status(state, client)
        return

    # 状態読み込み
    state = load_state()

    # 戦略切替
    if args.strategy and args.strategy != state.get("strategy"):
        print(f"  Switching strategy: {state.get('strategy')} -> {args.strategy}")
        state["strategy"] = args.strategy
        save_state(state)

    # ステータス表示のみ
    if args.status:
        print_status(state, client)
        return

    # 全ポジション強制決済
    if args.close_all:
        state = close_all_positions(state, client)
        print_status(state, client)
        return

    # 停止状態チェック
    if state.get("is_halted"):
        print(f"\n  *** HALTED: {state['halt_reason']} ***")
        print("  取引は停止中です。リセットするには --reset を実行してください。")
        print_status(state, client)
        return

    # --- メイン処理 ---
    strategy_key = state.get("strategy", "vol_div")
    print(f"  Strategy: {STRATEGY_MAP[strategy_key]['name']}")

    # 1. データ取得（シグナル判定用、Bybitの公開API）
    print("\n[Step 1] Fetching OHLCV data for signal evaluation...")
    config = state["config"]
    fetcher = CCXTFetcher(exchange=config["exchange"])
    data = fetcher.fetch(
        symbol=config["symbol"],
        period=f"{config['lookback_days']}d",
        interval=config["interval"],
    )
    print(f"  Data range: {data.index[0]} ~ {data.index[-1]}")

    # 2. 戦略シグナル判定
    print(f"\n[Step 2] Evaluating {STRATEGY_MAP[strategy_key]['name']} signal...")
    strategy, strategy_name = get_strategy(strategy_key)
    signal = evaluate_signal(data, strategy)
    signal_label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}
    print(f"  Signal: {signal} ({signal_label.get(signal, 'UNKNOWN')})")

    # 3. 取引実行
    print(f"\n[Step 3] Executing trade...")
    state = execute_live_trade(state, signal, client)
    save_state(state)

    # 4. ステータス表示
    print_status(state, client)

    print("[Live Trade] Done.\n")


if __name__ == "__main__":
    main()

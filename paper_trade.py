"""
paper_trade.py — 仮想通貨ペーパートレード環境（BTC/JPY）

国内取引所対応の仮想売買システム。
実際の発注はせず、シグナルに基づく仮想売買を記録する。

データソース:
  - OHLCV履歴: yfinance BTC-JPY（戦略シグナル計算用）
  - ライブ価格: bitFlyer BTC/JPY ticker（ccxt公開API、キー不要）
  - フォールバック: yfinance最終終値

対応戦略:
  - Volume Divergence（Optuna最適化済みパラメータ）— デフォルト
  - BB+RSI Combo / SMA Crossover / Momentum Pullback も切替可能

使い方:
  python paper_trade.py             # 日次実行（シグナル判定→仮想売買）
  python paper_trade.py --reset     # 状態リセット
  python paper_trade.py --summary   # サマリー表示のみ
  python paper_trade.py --strategy bb_rsi  # 戦略切替

cronで毎日実行:
  0 9 * * * cd /path/to/auto-trade && python3 paper_trade.py >> paper_trade_cron.log 2>&1
"""

# urllib3 v2 + LibreSSL環境でのNotOpenSSLWarning抑制（launchdエラーログ肥大化防止）
import warnings
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from trade_engine import TradeEngine
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy

# ==============================================================
# 設定
# ==============================================================

POSITIONS_FILE = PROJECT_ROOT / "paper_positions.json"
TRADE_LOG_FILE = PROJECT_ROOT / "paper_trade_log.json"
OPTIMIZED_PARAMS_FILE = PROJECT_ROOT / "optimized_params.json"

DEFAULT_CONFIG = {
    "yf_symbol": "BTC-JPY",         # yfinance用シンボル
    "bf_symbol": "BTC/JPY",         # bitFlyer ccxt用シンボル
    "interval": "1d",
    "lookback_period": "1y",         # yfinance period（200EMA等のために長めに取得）
    "initial_capital": 1_000_000.0,  # 初期資金（円）
    "commission_rate": 0.0015,       # bitFlyer現物手数料 0.15%
    "slippage_rate": 0.001,          # スリッページ 0.1%（JPYはスプレッドが広い）
    "risk_per_trade": 0.05,          # 1トレードあたり資金の5%
}

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
# 状態管理（paper_positions.json）
# ==============================================================

def load_state() -> dict:
    """ペーパートレード状態をJSONから読み込む。"""
    if POSITIONS_FILE.exists():
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    return create_initial_state()


def save_state(state: dict):
    """状態をJSONに保存する。"""
    with open(POSITIONS_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def create_initial_state() -> dict:
    return {
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "config": DEFAULT_CONFIG,
        "capital": DEFAULT_CONFIG["initial_capital"],
        "position": 0.0,          # BTC保有量（正=ロング, 0=ノーポジ）
        "entry_price": 0.0,       # エントリー価格（JPY）
        "entry_time": None,       # エントリー時刻
        "total_pnl": 0.0,         # 累計損益（JPY）
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "current_signal": 0,
        "strategy": "vol_div",
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
# データ取得
# ==============================================================

def fetch_ohlcv_yfinance(config: dict) -> pd.DataFrame:
    """yfinanceからBTC-JPYのOHLCVデータを取得する。"""
    symbol = config["yf_symbol"]
    period = config["lookback_period"]
    interval = config["interval"]

    print(f"  [yfinance] Fetching {symbol} ({period}, {interval})...")
    df = yf.download(symbol, period=period, interval=interval, progress=False)

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.dropna()
    print(f"  -> {len(df)} bars fetched ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def fetch_live_price_bitflyer(symbol: str = "BTC/JPY") -> dict:
    """bitFlyerからBTC/JPYのライブ価格を取得する。

    Returns:
        {"last": float, "bid": float, "ask": float, "source": str}
    """
    try:
        import ccxt
        bf = ccxt.bitflyer({"enableRateLimit": True})
        ticker = bf.fetch_ticker(symbol)
        return {
            "last": ticker["last"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "source": "bitflyer",
        }
    except Exception as e:
        print(f"  [WARN] bitFlyer価格取得失敗: {e}")
        return None


def get_current_price(config: dict, ohlcv_data: pd.DataFrame) -> tuple:
    """ライブ価格を取得する。bitFlyer → yfinance終値の順でフォールバック。

    Returns:
        (price: float, source: str)
    """
    # 1. bitFlyerライブ価格を試行
    ticker = fetch_live_price_bitflyer(config["bf_symbol"])
    if ticker and ticker["last"]:
        return ticker["last"], f"bitflyer (bid:{ticker['bid']:,.0f} / ask:{ticker['ask']:,.0f})"

    # 2. フォールバック: yfinance最終終値
    price = float(ohlcv_data["close"].iloc[-1])
    return price, "yfinance (last close)"


# ==============================================================
# 戦略
# ==============================================================

def get_strategy(strategy_key: str):
    """Optuna最適化パラメータを適用した戦略インスタンスを返す。"""
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
    """最新バーのシグナルを取得する。1=買い, -1=売り, 0=様子見"""
    signals = strategy.generate_signals(data)
    return int(signals.iloc[-1])


# ==============================================================
# ペーパートレード実行
# ==============================================================

def execute_paper_trade(state: dict, signal: int, price: float, price_source: str) -> dict:
    """シグナルに基づいて仮想売買を実行する。

    ルール:
    - signal=1  & position=0  → 買いエントリー
    - signal=-1 & position>0  → 決済（売り）
    - signal=-1 & position=0  → ショートエントリー
    - signal=1  & position<0  → ショート決済
    - signal=0                → 何もしない
    """
    config = state["config"]
    commission = config["commission_rate"] + config["slippage_rate"]
    trade_log = load_trade_log()
    trade_executed = False

    # --- ポジションクローズ判定 ---
    if state["position"] != 0:
        should_close = (
            (state["position"] > 0 and signal == -1) or
            (state["position"] < 0 and signal == 1)
        )
        if should_close:
            entry_price = state["entry_price"]
            position_size = abs(state["position"])
            direction = 1 if state["position"] > 0 else -1

            gross_pnl = direction * (price - entry_price) * position_size
            cost = price * position_size * commission
            net_pnl = gross_pnl - cost

            state["capital"] += (entry_price * position_size) + net_pnl  # 元本+損益を返却
            state["total_pnl"] += net_pnl
            state["total_trades"] += 1
            if net_pnl > 0:
                state["winning_trades"] += 1
            else:
                state["losing_trades"] += 1

            dir_label = "LONG" if direction == 1 else "SHORT"
            trade_log.append({
                "timestamp": datetime.now().isoformat(),
                "action": "CLOSE",
                "direction": dir_label,
                "entry_price": entry_price,
                "exit_price": price,
                "size_btc": position_size,
                "gross_pnl_jpy": round(gross_pnl),
                "cost_jpy": round(cost),
                "net_pnl_jpy": round(net_pnl),
                "capital_after_jpy": round(state["capital"]),
                "price_source": price_source,
            })

            print(f"  [CLOSE] {dir_label} @ {price:,.0f} JPY")
            print(f"          Entry: {entry_price:,.0f} JPY | PnL: {net_pnl:+,.0f} JPY")

            state["position"] = 0.0
            state["entry_price"] = 0.0
            state["entry_time"] = None
            trade_executed = True

    # --- エントリー判定 ---
    if signal != 0 and state["position"] == 0:
        # スポット口座ではショート禁止（live_trade.pyと評価条件を統一）
        # ペーパーでショートを許可すると、liveで再現不能な成績が混ざる
        if signal == -1:
            print(f"  [BLOCKED] ショートエントリーは禁止（live環境と統一）")
            save_trade_log(trade_log)
            return state

        risk_pct = config["risk_per_trade"]
        risk_capital = state["capital"] * risk_pct
        position_size = risk_capital / price  # BTC数量
        cost = price * position_size * commission

        if signal == 1:
            state["position"] = position_size
            action = "BUY (LONG)"
        else:
            state["position"] = -position_size
            action = "SELL (SHORT)"

        state["entry_price"] = price
        state["entry_time"] = datetime.now().isoformat()
        state["capital"] -= (price * position_size + cost)  # 元本+手数料を差し引く

        trade_log.append({
            "timestamp": datetime.now().isoformat(),
            "action": "ENTRY",
            "direction": "LONG" if signal == 1 else "SHORT",
            "price_jpy": price,
            "size_btc": position_size,
            "value_jpy": round(price * position_size),
            "cost_jpy": round(cost),
            "capital_after_jpy": round(state["capital"]),
            "price_source": price_source,
        })

        print(f"  [ENTRY] {action} @ {price:,.0f} JPY x {position_size:.8f} BTC")
        print(f"          Value: {price * position_size:,.0f} JPY | Cost: {cost:,.0f} JPY")
        trade_executed = True

    if not trade_executed:
        pos_label = f"{state['position']:.8f} BTC" if state["position"] != 0 else "none"
        print(f"  [HOLD] signal={signal}, position={pos_label}")

    state["current_signal"] = signal
    state["last_updated"] = datetime.now().isoformat()
    save_trade_log(trade_log)
    return state


# ==============================================================
# サマリー表示
# ==============================================================

def print_summary(state: dict, current_price: float = None, price_source: str = ""):
    """ペーパートレードのサマリーを表示する。"""
    print("\n" + "=" * 60)
    print("  PAPER TRADE SUMMARY  (BTC/JPY)")
    print("=" * 60)

    strategy_name = STRATEGY_MAP.get(state.get("strategy", "vol_div"), {}).get("name", "Unknown")
    print(f"  Strategy     : {strategy_name}")
    print(f"  Started      : {state['created_at'][:10]}")
    print(f"  Last Updated : {state['last_updated'][:10]}")
    print()

    # --- 資産状況 ---
    initial = state["config"]["initial_capital"]
    capital = state["capital"]
    unrealized = 0.0
    position_value = 0.0

    if current_price and state["position"] != 0:
        direction = 1 if state["position"] > 0 else -1
        position_value = abs(state["position"]) * current_price
        unrealized = direction * (current_price - state["entry_price"]) * abs(state["position"])

    # 総資産 = 現金 + ポジション清算価値（証拠金 + 含み損益）
    # ショートでは position_value（=現在価格×数量）ではなく、
    # 決済時に返ってくる金額（エントリー価格×数量 + 含み損益）を使う
    if state["position"] != 0:
        position_liquidation_value = abs(state["position"]) * state["entry_price"] + unrealized
    else:
        position_liquidation_value = 0.0
    total_value = capital + position_liquidation_value
    total_return = ((total_value / initial) - 1) * 100

    print(f"  Initial Capital  : {initial:>14,.0f} JPY")
    print(f"  Cash             : {capital:>14,.0f} JPY")
    print(f"  Position Value   : {position_value:>14,.0f} JPY")
    print(f"  Unrealized PnL   : {unrealized:>+14,.0f} JPY")
    print(f"  Total Value      : {total_value:>14,.0f} JPY")
    print(f"  Total Return     : {total_return:>+13.2f}%")
    print(f"  Realized PnL     : {state['total_pnl']:>+14,.0f} JPY")
    print()

    # --- トレード統計 ---
    total_trades = state["total_trades"]
    win = state["winning_trades"]
    lose = state["losing_trades"]
    win_rate = (win / total_trades * 100) if total_trades > 0 else 0.0

    print(f"  Total Trades : {total_trades}")
    print(f"  Wins / Losses: {win} / {lose}")
    print(f"  Win Rate     : {win_rate:.1f}%")
    print()

    # --- ポジション ---
    if state["position"] > 0:
        pos_str = f"LONG {abs(state['position']):.8f} BTC @ {state['entry_price']:,.0f} JPY"
    elif state["position"] < 0:
        pos_str = f"SHORT {abs(state['position']):.8f} BTC @ {state['entry_price']:,.0f} JPY"
    else:
        pos_str = "NO POSITION"

    print(f"  Position     : {pos_str}")
    print(f"  Last Signal  : {state['current_signal']}")

    if current_price:
        print(f"  Current Price: {current_price:>14,.0f} JPY ({price_source})")

    print("=" * 60)

    # --- 最近のトレードログ ---
    trade_log = load_trade_log()
    if trade_log:
        print("\n  Recent Trades (last 5):")
        print("  " + "-" * 56)
        for t in trade_log[-5:]:
            ts = t["timestamp"][:16]
            if t["action"] == "ENTRY":
                price_val = t.get("price_jpy", 0)
                print(f"  {ts} | ENTRY {t['direction']:5} @ {price_val:>12,.0f} JPY")
            else:
                pnl = t.get("net_pnl_jpy", 0)
                exit_p = t.get("exit_price", 0)
                print(f"  {ts} | CLOSE {t['direction']:5} @ {exit_p:>12,.0f} JPY  PnL: {pnl:>+10,.0f}")
        print()


# ==============================================================
# TradeEngine 準拠クラス
# ==============================================================

class PaperTradeEngine(TradeEngine):
    """ペーパートレードエンジン。TradeEngine基底クラスを継承。

    実際の発注はせず、仮想売買を記録する。
    LiveTradeEngineと同じインターフェースで切り替え可能。
    """

    def __init__(self, strategy_key: str = "vol_div"):
        super().__init__(strategy_key)
        self.state = load_state()
        if self.state.get("strategy") != strategy_key:
            self.state["strategy"] = strategy_key
            save_state(self.state)

    def run(self) -> dict:
        config = self.state["config"]
        data = fetch_ohlcv_yfinance(config)
        if len(data) < 50:
            print("  [ERROR] Insufficient data.")
            return self.state
        current_price, price_source = get_current_price(config, data)
        strategy, _ = get_strategy(self.strategy_key)
        signal = evaluate_signal(data, strategy)
        self.state = execute_paper_trade(self.state, signal, current_price, price_source)
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
        return {
            "capital": self.state["capital"],
            "total_pnl": self.state["total_pnl"],
            "total_trades": self.state["total_trades"],
            "winning_trades": self.state["winning_trades"],
            "losing_trades": self.state["losing_trades"],
        }

    def market_buy(self, amount: float) -> dict:
        config = self.state["config"]
        current_price, source = get_current_price(config,
            fetch_ohlcv_yfinance(config))
        self.state = execute_paper_trade(self.state, 1, current_price, source)
        save_state(self.state)
        return {"status": "paper", "side": "buy", "price": current_price}

    def market_sell(self, amount: float) -> dict:
        config = self.state["config"]
        current_price, source = get_current_price(config,
            fetch_ohlcv_yfinance(config))
        self.state = execute_paper_trade(self.state, -1, current_price, source)
        save_state(self.state)
        return {"status": "paper", "side": "sell", "price": current_price}

    def emergency_close(self) -> dict:
        if self.state["position"] == 0:
            return self.state
        config = self.state["config"]
        current_price, source = get_current_price(config,
            fetch_ohlcv_yfinance(config))
        close_signal = -1 if self.state["position"] > 0 else 1
        self.state = execute_paper_trade(self.state, close_signal, current_price, source)
        save_state(self.state)
        return self.state

    def status(self):
        config = self.state["config"]
        price, source = None, ""
        ticker = fetch_live_price_bitflyer(config["bf_symbol"])
        if ticker:
            price, source = ticker["last"], "bitflyer"
        print_summary(self.state, price, source)

    def reset(self):
        self.state = create_initial_state()
        self.state["strategy"] = self.strategy_key
        save_state(self.state)
        save_trade_log([])


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(description="Paper Trading System (BTC/JPY)")
    parser.add_argument("--reset", action="store_true", help="Reset state and start fresh")
    parser.add_argument("--summary", action="store_true", help="Show summary only")
    parser.add_argument("--strategy", type=str, default=None,
                        help=f"Strategy: {list(STRATEGY_MAP.keys())}")
    args = parser.parse_args()

    print("\n[Paper Trade] Starting...")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # --- リセット ---
    if args.reset:
        state = create_initial_state()
        if args.strategy:
            state["strategy"] = args.strategy
        save_state(state)
        save_trade_log([])
        print("  State reset to initial values.")
        print_summary(state)
        return

    # --- 状態読み込み ---
    state = load_state()

    if args.strategy and args.strategy != state.get("strategy"):
        print(f"  Switching strategy: {state.get('strategy')} -> {args.strategy}")
        state["strategy"] = args.strategy
        save_state(state)

    strategy_key = state.get("strategy", "vol_div")

    # --- サマリーのみ ---
    if args.summary:
        config = state["config"]
        price, source = None, ""
        ticker = fetch_live_price_bitflyer(config["bf_symbol"])
        if ticker:
            price, source = ticker["last"], "bitflyer"
        print_summary(state, price, source)
        return

    # --- メイン処理 ---
    config = state["config"]
    print(f"  Strategy: {STRATEGY_MAP[strategy_key]['name']}")
    print(f"  Symbol: {config['yf_symbol']} (data) / {config['bf_symbol']} (live)")

    # 1. OHLCV取得（yfinance BTC-JPY）
    print("\n[Step 1] Fetching OHLCV data (yfinance)...")
    data = fetch_ohlcv_yfinance(config)

    if len(data) < 50:
        print("  [ERROR] Insufficient data for strategy calculation. Aborting.")
        return

    # 2. ライブ価格取得（bitFlyer → yfinance fallback）
    print("\n[Step 2] Fetching live price...")
    current_price, price_source = get_current_price(config, data)
    print(f"  Price: {current_price:,.0f} JPY ({price_source})")

    # 3. 戦略シグナル判定
    print(f"\n[Step 3] Evaluating {STRATEGY_MAP[strategy_key]['name']} signal...")
    strategy, strategy_name = get_strategy(strategy_key)
    signal = evaluate_signal(data, strategy)
    signal_label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}
    print(f"  Signal: {signal} ({signal_label.get(signal, 'UNKNOWN')})")

    # 4. ペーパートレード実行
    print(f"\n[Step 4] Executing paper trade...")
    state = execute_paper_trade(state, signal, current_price, price_source)
    save_state(state)

    # 5. サマリー表示
    print_summary(state, current_price, price_source)

    print("[Paper Trade] Done.\n")


if __name__ == "__main__":
    main()

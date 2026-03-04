"""
crypto_monitor.py — 仮想通貨自律監視エージェント

ローカルLLM（Ollama qwen2.5:7b）を使った仮想通貨の自律監視システム。
1時間毎にcronで実行し、以下を自動的に行う:

1. bitFlyer公開APIでBTC/JPY価格取得
2. 全戦略のシグナル判定
3. ペーパーポジション管理（paper_positions.json経由）
4. パフォーマンス記録（performance_log.json）
5. Ollama LLMに分析依頼 → 日次レポート生成

卒業条件（実弾投入推奨の閾値）:
  - ペーパートレード期間: 最低2週間
  - 勝率: 40%以上
  - ローリングSharpe: 0.5以上
  - 最大DD: -15%以内
  - バックテスト結果との乖離: +-20%以内

使い方:
  python crypto_monitor.py              # 通常実行（シグナル判定+ペーパートレード+記録）
  python crypto_monitor.py --report     # LLM日次レポート生成
  python crypto_monitor.py --status     # 卒業条件チェック
  python crypto_monitor.py --full       # 全工程実行（通常+レポート+卒業チェック）

cron設定例:
  # 毎時0分に監視実行
  0 * * * * cd /path/to/auto-trade && python3 crypto_monitor.py >> crypto_monitor.log 2>&1
  # 毎日9時にフルレポート
  0 9 * * * cd /path/to/auto-trade && python3 crypto_monitor.py --full >> crypto_monitor.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import sys
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# ==============================================================
# 設定読み込み
# ==============================================================

CONFIG_FILE = PROJECT_ROOT / "crypto_config.json"
PERFORMANCE_LOG_FILE = PROJECT_ROOT / "performance_log.json"
DAILY_REPORT_FILE = PROJECT_ROOT / "crypto_daily_report.md"
POSITIONS_FILE = PROJECT_ROOT / "paper_positions.json"
TRADE_LOG_FILE = PROJECT_ROOT / "paper_trade_log.json"


def load_config() -> dict:
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def load_json(path: Path) -> dict | list:
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {} if path.suffix == ".json" else []


def save_json(path: Path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# 価格取得
# ==============================================================

def fetch_live_price(config: dict) -> dict:
    """bitFlyerからBTC/JPY価格を取得する。"""
    try:
        import ccxt
        bf = ccxt.bitflyer({"enableRateLimit": True})
        ticker = bf.fetch_ticker(config["exchange"]["symbol"])
        return {
            "price": ticker["last"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "spread": ticker["ask"] - ticker["bid"],
            "timestamp": datetime.now().isoformat(),
            "source": "bitflyer",
        }
    except Exception as e:
        print(f"  [WARN] bitFlyer取得失敗: {e}")
        return None


def fetch_ohlcv(config: dict) -> pd.DataFrame:
    """yfinanceからBTC-JPYのOHLCVを取得する。"""
    import yfinance as yf
    symbol = config["exchange"]["yf_symbol"]
    period = config["monitoring"]["lookback_period"]
    interval = config["monitoring"]["data_interval"]

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.dropna()
    return df


# ==============================================================
# 戦略シグナル判定
# ==============================================================

def evaluate_all_strategies(data: pd.DataFrame, config: dict) -> dict:
    """全戦略のシグナルを一括判定する。"""
    from strategies.volume_divergence import VolumeDivergenceStrategy
    from strategies.bb_rsi_combo import BBRSIComboStrategy
    from strategies.sma_crossover import SMACrossoverStrategy
    from strategies.momentum_pullback import MomentumPullbackStrategy

    optimized_params_file = PROJECT_ROOT / "optimized_params.json"
    opt_params = {}
    if optimized_params_file.exists():
        with open(optimized_params_file, "r") as f:
            opt_params = json.load(f)

    strategy_classes = {
        "vol_div": (VolumeDivergenceStrategy, "vol_div"),
        "bb_rsi": (BBRSIComboStrategy, "bb_rsi"),
        "sma": (SMACrossoverStrategy, "sma"),
        "mom_pb": (MomentumPullbackStrategy, "mom_pb"),
    }

    results = {}
    for key, (cls, param_key) in strategy_classes.items():
        try:
            params = opt_params.get(param_key, {})
            strategy = cls(params=params)
            signals = strategy.generate_signals(data)
            latest = int(signals.iloc[-1])
            signal_label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}
            results[key] = {
                "signal": latest,
                "label": signal_label.get(latest, "UNKNOWN"),
                "name": strategy.meta.name,
            }
        except Exception as e:
            results[key] = {"signal": 0, "label": "ERROR", "name": key, "error": str(e)}

    return results


# ==============================================================
# パフォーマンス記録
# ==============================================================

def record_performance(price_data: dict, signals: dict, positions: dict):
    """パフォーマンスログに1レコード追加する。"""
    log = load_json(PERFORMANCE_LOG_FILE)
    if not isinstance(log, list):
        log = []

    # 含み損益計算
    unrealized_pnl = 0.0
    if positions.get("position", 0) != 0 and price_data:
        direction = 1 if positions["position"] > 0 else -1
        unrealized_pnl = direction * (price_data["price"] - positions["entry_price"]) * abs(positions["position"])

    capital = positions.get("capital", 0)
    position_value = abs(positions.get("position", 0)) * price_data["price"] if price_data else 0
    total_value = capital + position_value

    record = {
        "timestamp": datetime.now().isoformat(),
        "price_jpy": price_data["price"] if price_data else None,
        "spread": price_data.get("spread") if price_data else None,
        "signals": {k: v["label"] for k, v in signals.items()},
        "position_btc": positions.get("position", 0),
        "entry_price": positions.get("entry_price", 0),
        "capital_jpy": round(capital),
        "position_value_jpy": round(position_value),
        "total_value_jpy": round(total_value),
        "unrealized_pnl_jpy": round(unrealized_pnl),
        "realized_pnl_jpy": round(positions.get("total_pnl", 0)),
        "total_trades": positions.get("total_trades", 0),
        "winning_trades": positions.get("winning_trades", 0),
    }

    log.append(record)
    save_json(PERFORMANCE_LOG_FILE, log)
    return record


# ==============================================================
# 卒業条件チェック
# ==============================================================

def check_graduation(config: dict, positions: dict) -> dict:
    """卒業条件（実弾投入推奨の閾値）をチェックする。"""
    criteria = config["graduation_criteria"]
    log = load_json(PERFORMANCE_LOG_FILE)
    if not isinstance(log, list) or len(log) == 0:
        return {"ready": False, "reason": "No performance data yet", "checks": {}}

    # 運用日数
    first_ts = datetime.fromisoformat(log[0]["timestamp"])
    days_running = (datetime.now() - first_ts).days

    # 勝率
    total_trades = positions.get("total_trades", 0)
    winning_trades = positions.get("winning_trades", 0)
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    # ローリングSharpe（日次リターンから計算）
    values = [r["total_value_jpy"] for r in log if r.get("total_value_jpy")]
    rolling_sharpe = 0.0
    if len(values) >= 5:
        returns = pd.Series(values).pct_change().dropna()
        if returns.std() > 0:
            rolling_sharpe = (returns.mean() / returns.std()) * np.sqrt(365)

    # 最大ドローダウン
    max_dd = 0.0
    if values:
        peak = values[0]
        for v in values:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd

    # チェック結果
    checks = {
        "min_days": {
            "required": criteria["min_days"],
            "actual": days_running,
            "passed": days_running >= criteria["min_days"],
        },
        "win_rate": {
            "required": f">= {criteria['min_win_rate']}%",
            "actual": f"{win_rate:.1f}%",
            "passed": win_rate >= criteria["min_win_rate"],
        },
        "rolling_sharpe": {
            "required": f">= {criteria['min_rolling_sharpe']}",
            "actual": f"{rolling_sharpe:.2f}",
            "passed": rolling_sharpe >= criteria["min_rolling_sharpe"],
        },
        "max_drawdown": {
            "required": f"> {criteria['max_drawdown_pct']}%",
            "actual": f"{max_dd:.1f}%",
            "passed": max_dd > criteria["max_drawdown_pct"],
        },
    }

    all_passed = all(c["passed"] for c in checks.values())

    return {
        "ready": all_passed,
        "days_running": days_running,
        "checks": checks,
    }


# ==============================================================
# Ollama LLM 分析
# ==============================================================

def query_ollama(prompt: str, config: dict) -> str:
    """OllamaのAPIにプロンプトを送り、応答を返す。"""
    ollama_config = config["ollama"]
    url = f"{ollama_config['base_url']}/api/generate"

    try:
        resp = requests.post(
            url,
            json={
                "model": ollama_config["model"],
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 1024,
                },
            },
            timeout=ollama_config["timeout_seconds"],
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        return "[ERROR] Ollama API接続失敗。Ollamaが起動しているか確認してください。"
    except Exception as e:
        return f"[ERROR] Ollama API呼び出し失敗: {e}"


def generate_daily_report(config: dict, price_data: dict, signals: dict,
                          positions: dict, graduation: dict) -> str:
    """LLMに日次分析レポートを生成させる。"""
    # コンテキスト構築
    trade_log = load_json(TRADE_LOG_FILE)
    recent_trades = trade_log[-5:] if isinstance(trade_log, list) else []

    perf_log = load_json(PERFORMANCE_LOG_FILE)
    recent_perf = perf_log[-24:] if isinstance(perf_log, list) else []  # 直近24レコード

    price_str = f"{price_data['price']:,.0f} JPY" if price_data else "取得失敗"
    spread_str = f"{price_data['spread']:,.0f} JPY" if price_data and price_data.get('spread') else "N/A"

    signals_str = "\n".join([
        f"  - {v['name']}: {v['label']}" for v in signals.values()
    ])

    pos_str = "なし"
    if positions.get("position", 0) > 0:
        pos_str = f"LONG {abs(positions['position']):.8f} BTC @ {positions['entry_price']:,.0f} JPY"
    elif positions.get("position", 0) < 0:
        pos_str = f"SHORT {abs(positions['position']):.8f} BTC @ {positions['entry_price']:,.0f} JPY"

    total_trades = positions.get("total_trades", 0)
    win = positions.get("winning_trades", 0)
    win_rate = f"{win/total_trades*100:.1f}%" if total_trades > 0 else "N/A"

    grad_str = "合格" if graduation.get("ready") else "未達"
    grad_details = "\n".join([
        f"  - {k}: {'PASS' if v['passed'] else 'FAIL'} (required: {v['required']}, actual: {v['actual']})"
        for k, v in graduation.get("checks", {}).items()
    ])

    # 直近の価格推移
    price_history = ""
    if recent_perf:
        for r in recent_perf[-6:]:
            ts = r["timestamp"][:16]
            p = r.get("price_jpy", 0)
            if p:
                price_history += f"  {ts}: {p:,.0f} JPY\n"

    prompt = f"""あなたは仮想通貨トレーディングの分析AIです。以下のデータを分析し、日次レポートを日本語で作成してください。

## 現在の市場データ
- BTC/JPY現在価格: {price_str}
- スプレッド: {spread_str}

## 直近の価格推移
{price_history}

## 戦略シグナル（Optuna最適化済み）
{signals_str}

## ポジション状況
- 現在ポジション: {pos_str}
- 累計損益: {positions.get('total_pnl', 0):+,.0f} JPY
- 総トレード数: {total_trades}回（勝率: {win_rate}）

## 卒業条件チェック（実弾投入推奨の閾値）
- 総合判定: {grad_str}
{grad_details}

## 直近のトレード履歴
{json.dumps(recent_trades[-3:], indent=2, ensure_ascii=False) if recent_trades else "なし"}

以下の構成でレポートを作成してください:
1. 市場状況の要約（2-3行）
2. 戦略パフォーマンスの評価（シグナルの一致/不一致に注目）
3. リスク評価（ドローダウン、スプレッド変動）
4. 卒業条件の進捗と課題
5. 次のアクション提案（1-2行）

簡潔に、数値を交えて記述してください。"""

    print("  [Ollama] Generating daily report...")
    response = query_ollama(prompt, config)
    return response


def save_daily_report(report_text: str, price_data: dict, signals: dict,
                      positions: dict, graduation: dict):
    """日次レポートをMarkdownファイルに保存する。"""
    now = datetime.now()
    header = f"""# Crypto Daily Report — {now.strftime('%Y-%m-%d %H:%M')}

**Generated by**: Ollama qwen2.5:7b (local LLM)
**Strategy**: Volume Divergence (Optuna optimized)
**Symbol**: BTC/JPY

---

## Quick Stats

| Item | Value |
|------|-------|
| Price | {price_data['price']:,.0f} JPY |
| Position | {'LONG' if positions.get('position',0) > 0 else 'SHORT' if positions.get('position',0) < 0 else 'NONE'} |
| Realized PnL | {positions.get('total_pnl', 0):+,.0f} JPY |
| Trades | {positions.get('total_trades', 0)} |
| Win Rate | {positions.get('winning_trades',0)}/{positions.get('total_trades',0)} |
| Graduation | {'READY' if graduation.get('ready') else 'NOT YET'} |

---

## LLM Analysis

{report_text}

---

*Report generated at {now.isoformat()}*
""" if price_data else f"""# Crypto Daily Report — {now.strftime('%Y-%m-%d %H:%M')}

Price data unavailable. Report skipped.
"""

    with open(DAILY_REPORT_FILE, "w") as f:
        f.write(header)
    print(f"  Report saved to: {DAILY_REPORT_FILE.name}")


# ==============================================================
# メイン処理
# ==============================================================

def run_monitor(config: dict, run_paper_trade: bool = True):
    """監視メインループ（1回分の実行）。"""
    print(f"\n[Monitor] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 価格取得
    print("[1/4] Fetching price...")
    price_data = fetch_live_price(config)
    if price_data:
        print(f"  BTC/JPY: {price_data['price']:,.0f} (spread: {price_data['spread']:,.0f})")
    else:
        print("  [WARN] Price fetch failed, using yfinance fallback")
        data = fetch_ohlcv(config)
        price_data = {
            "price": float(data["close"].iloc[-1]),
            "bid": 0, "ask": 0, "spread": 0,
            "timestamp": datetime.now().isoformat(),
            "source": "yfinance",
        }
        print(f"  BTC/JPY: {price_data['price']:,.0f} (yfinance)")

    # 2. OHLCV + シグナル判定
    print("[2/4] Evaluating strategies...")
    data = fetch_ohlcv(config)
    signals = evaluate_all_strategies(data, config)
    for key, sig in signals.items():
        marker = "*" if sig["label"] != "NEUTRAL" else " "
        print(f"  {marker} {sig['name']:25} -> {sig['label']}")

    # 3. ペーパートレード実行
    positions = load_json(POSITIONS_FILE)
    if not positions:
        print("  [WARN] No paper_positions.json found. Run paper_trade.py --reset first.")
        positions = {"position": 0, "entry_price": 0, "capital": config["paper_trade"]["initial_capital_jpy"],
                     "total_pnl": 0, "total_trades": 0, "winning_trades": 0, "losing_trades": 0}

    if run_paper_trade:
        print("[3/4] Executing paper trade...")
        # paper_trade.pyのmain()を直接呼ぶ代わりに、サブプロセスで実行
        import subprocess
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "paper_trade.py")],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            timeout=120,
        )
        if result.returncode == 0:
            # 更新されたpositionsを再読み込み
            positions = load_json(POSITIONS_FILE)
            # paper_trade.pyの出力から重要な行だけ抽出
            for line in result.stdout.split("\n"):
                if any(tag in line for tag in ["[ENTRY]", "[CLOSE]", "[HOLD]", "Signal:"]):
                    print(f"  {line.strip()}")
        else:
            print(f"  [ERROR] paper_trade.py failed: {result.stderr[:200]}")
    else:
        print("[3/4] Paper trade skipped (monitor-only mode)")

    # 4. パフォーマンス記録
    print("[4/4] Recording performance...")
    record = record_performance(price_data, signals, positions)
    print(f"  Total value: {record['total_value_jpy']:,.0f} JPY | PnL: {record['realized_pnl_jpy']:+,.0f}")

    return price_data, signals, positions


def run_report(config: dict, price_data: dict = None, signals: dict = None, positions: dict = None):
    """LLM日次レポートを生成する。"""
    print("\n[Report] Generating LLM daily report...")

    if not price_data:
        price_data = fetch_live_price(config)
    if not signals:
        data = fetch_ohlcv(config)
        signals = evaluate_all_strategies(data, config)
    if not positions:
        positions = load_json(POSITIONS_FILE)

    graduation = check_graduation(config, positions)
    report_text = generate_daily_report(config, price_data, signals, positions, graduation)
    save_daily_report(report_text, price_data, signals, positions, graduation)

    print("\n--- LLM Report Preview ---")
    # 最初の500文字だけ表示
    preview = report_text[:500]
    print(preview)
    if len(report_text) > 500:
        print(f"... ({len(report_text)} chars total)")
    print("--- End Preview ---\n")


def run_status(config: dict):
    """卒業条件チェックを表示する。"""
    positions = load_json(POSITIONS_FILE)
    graduation = check_graduation(config, positions)

    print("\n" + "=" * 50)
    print("  GRADUATION STATUS CHECK")
    print("=" * 50)

    if graduation.get("ready"):
        print("  *** READY FOR LIVE TRADING ***")
        print("  All graduation criteria met!")
    else:
        print(f"  Status: NOT READY (Day {graduation.get('days_running', 0)})")

    print()
    for name, check in graduation.get("checks", {}).items():
        status = "PASS" if check["passed"] else "FAIL"
        print(f"  [{status:4}] {name:20} required: {check['required']:>15}  actual: {check['actual']:>15}")

    print("=" * 50)

    if graduation.get("ready"):
        print("\n  >>> ALERT: 卒業条件を全て満たしました。")
        print("  >>> 上様の承認後、少額（初回1万円）でライブ取引を開始できます。")
    print()


def main():
    parser = argparse.ArgumentParser(description="Crypto Autonomous Monitoring Agent")
    parser.add_argument("--report", action="store_true", help="Generate LLM daily report")
    parser.add_argument("--status", action="store_true", help="Check graduation criteria")
    parser.add_argument("--full", action="store_true", help="Full run: monitor + report + status")
    parser.add_argument("--no-trade", action="store_true", help="Monitor only, skip paper trade")
    args = parser.parse_args()

    config = load_config()

    if args.full:
        price_data, signals, positions = run_monitor(config, run_paper_trade=True)
        run_report(config, price_data, signals, positions)
        run_status(config)
    elif args.report:
        run_report(config)
    elif args.status:
        run_status(config)
    else:
        run_monitor(config, run_paper_trade=not args.no_trade)


if __name__ == "__main__":
    main()

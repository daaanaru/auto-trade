"""
マルチマーケットバックテスト

全7戦略 x 複数銘柄（日本株・FX・米国株・暗号資産）でバックテストを実行し、
戦略の汎用性を検証する。

使い方:
    python run_multi_market.py                    # 全銘柄・全戦略
    python run_multi_market.py --market jp        # 日本株のみ
    python run_multi_market.py --market fx        # FXのみ
    python run_multi_market.py --market us        # 米国株のみ
    python run_multi_market.py --market crypto    # 暗号資産のみ
    python run_multi_market.py --period 1y        # 期間変更
"""

import argparse
import json
import os
from datetime import datetime

from engine import BacktestEngine, BacktestConfig, YFinanceFetcher
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.rsi_reversion import RSIMeanReversionStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy
from strategies.order_block import OrderBlockStrategy


# ==============================================================
# 銘柄定義
# ==============================================================

MARKETS = {
    "jp": {
        "name": "Japanese Stocks",
        "symbols": [
            ("7203.T", "Toyota"),
            ("9984.T", "SoftBank Group"),
            ("8306.T", "MUFG"),
            ("6758.T", "Sony"),
        ],
    },
    "fx": {
        "name": "FX (Forex)",
        "symbols": [
            ("USDJPY=X", "USD/JPY"),
            ("EURJPY=X", "EUR/JPY"),
            ("GBPJPY=X", "GBP/JPY"),
        ],
    },
    "us": {
        "name": "US Stocks",
        "symbols": [
            ("AAPL", "Apple"),
            ("NVDA", "NVIDIA"),
            ("SPY", "S&P500 ETF"),
        ],
    },
    "crypto": {
        "name": "Crypto",
        "symbols": [
            ("BTC-USD", "Bitcoin"),
        ],
    },
}


# ==============================================================
# 戦略マッピング（Optuna最適化パラメータ対応）
# ==============================================================

STRATEGY_KEY_MAP = {
    "sma": ("SMA Crossover", SMACrossoverStrategy),
    "rsi": ("RSI Reversion", RSIMeanReversionStrategy),
    "bb_rsi": ("BB+RSI Combo", BBRSIComboStrategy),
    "monthly": ("Monthly Momentum", MonthlyMomentumStrategy),
    "vol_div": ("Volume Divergence", VolumeDivergenceStrategy),
    "mom_pb": ("Momentum Pullback", MomentumPullbackStrategy),
    "order_block": ("Order Block", OrderBlockStrategy),
}


def load_optimized_params():
    """optimized_params.jsonから最適パラメータを読み込む。なければデフォルト。"""
    params_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimized_params.json")
    if os.path.exists(params_path):
        with open(params_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def create_strategies(opt_params):
    """最適化パラメータで全戦略を生成する。"""
    strategies = []
    for key, (name, cls) in STRATEGY_KEY_MAP.items():
        params = opt_params.get(key, {})
        strategies.append((key, name, cls(params=params if params else None)))
    return strategies


def run_multi_market(markets_to_run, period, opt_params):
    """マルチマーケットバックテストを実行する。"""
    fetcher = YFinanceFetcher()
    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))
    strategies = create_strategies(opt_params)

    all_results = []

    for market_key in markets_to_run:
        market = MARKETS[market_key]
        print(f"\n{'#'*70}")
        print(f"  Market: {market['name']}")
        print(f"{'#'*70}")

        for symbol, display_name in market["symbols"]:
            print(f"\n--- {display_name} ({symbol}) ---")
            try:
                data = fetcher.fetch(symbol, period=period)
            except Exception as e:
                print(f"  ERROR: Failed to fetch {symbol}: {e}")
                continue

            if len(data) < 50:
                print(f"  SKIP: Not enough data ({len(data)} bars)")
                continue

            has_volume = data["volume"].sum() > 0

            for key, name, strategy in strategies:
                # 出来高が必要な戦略はvolumeが0の銘柄ではスキップ
                needs_volume = key in ("monthly", "vol_div", "mom_pb")
                if needs_volume and not has_volume:
                    print(f"  SKIP {name}: No volume data for {symbol}")
                    all_results.append({
                        "market": market_key,
                        "symbol": symbol,
                        "display_name": display_name,
                        "strategy_key": key,
                        "strategy_name": name,
                        "status": "skipped",
                        "reason": "no_volume",
                    })
                    continue

                try:
                    result = engine.run(strategy, data, verbose=False)
                    all_results.append({
                        "market": market_key,
                        "symbol": symbol,
                        "display_name": display_name,
                        "strategy_key": key,
                        "strategy_name": name,
                        "status": "ok",
                        "annual_return": round(result.annual_return, 2),
                        "max_drawdown": round(result.max_drawdown, 2),
                        "sharpe_ratio": round(result.sharpe_ratio, 2),
                        "win_rate": round(result.win_rate, 1),
                        "total_trades": result.total_trades,
                    })
                except Exception as e:
                    print(f"  ERROR {name} on {symbol}: {e}")
                    all_results.append({
                        "market": market_key,
                        "symbol": symbol,
                        "display_name": display_name,
                        "strategy_key": key,
                        "strategy_name": name,
                        "status": "error",
                        "reason": str(e),
                    })

    return all_results


def print_results_table(results):
    """結果を見やすいテーブルで出力する。"""
    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        print("\nNo results to display.")
        return

    print(f"\n{'='*90}")
    print("  MULTI-MARKET BACKTEST RESULTS (Optimized Parameters)")
    print(f"{'='*90}")
    print(f"{'Symbol':<14} {'Strategy':<22} {'Return%':>9} {'MaxDD%':>9} {'Sharpe':>8} {'Trades':>8}")
    print(f"{'-'*70}")

    current_symbol = None
    for r in ok_results:
        if r["symbol"] != current_symbol:
            if current_symbol is not None:
                print(f"{'-'*70}")
            current_symbol = r["symbol"]
            print(f"\n  [{r['display_name']}]")

        ret_str = f"{r['annual_return']:+.1f}%"
        dd_str = f"{r['max_drawdown']:.1f}%"
        sharpe_str = f"{r['sharpe_ratio']:.2f}"
        marker = " *" if r["sharpe_ratio"] > 0.5 else ""

        print(
            f"{'':14} {r['strategy_name']:<22} {ret_str:>9} {dd_str:>9} "
            f"{sharpe_str:>8} {r['total_trades']:>8}{marker}"
        )


def print_strategy_ranking(results):
    """戦略ごとの平均パフォーマンスランキング。"""
    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        return

    # 戦略ごとに集計
    from collections import defaultdict
    stats = defaultdict(lambda: {"returns": [], "sharpes": [], "drawdowns": []})

    for r in ok_results:
        key = r["strategy_name"]
        stats[key]["returns"].append(r["annual_return"])
        stats[key]["sharpes"].append(r["sharpe_ratio"])
        stats[key]["drawdowns"].append(r["max_drawdown"])

    print(f"\n{'='*70}")
    print("  STRATEGY RANKING (Average across all markets)")
    print(f"{'='*70}")
    print(f"{'Strategy':<22} {'Avg Return%':>12} {'Avg MaxDD%':>12} {'Avg Sharpe':>12} {'Markets':>8}")
    print(f"{'-'*66}")

    rankings = []
    for name, s in stats.items():
        avg_ret = sum(s["returns"]) / len(s["returns"])
        avg_sharpe = sum(s["sharpes"]) / len(s["sharpes"])
        avg_dd = sum(s["drawdowns"]) / len(s["drawdowns"])
        rankings.append((name, avg_ret, avg_dd, avg_sharpe, len(s["returns"])))

    rankings.sort(key=lambda x: x[3], reverse=True)

    for name, avg_ret, avg_dd, avg_sharpe, count in rankings:
        marker = " ***" if avg_sharpe > 0.3 else ""
        print(
            f"{name:<22} {avg_ret:>+11.1f}% {avg_dd:>11.1f}% "
            f"{avg_sharpe:>12.2f} {count:>8}{marker}"
        )


def save_results(results, markets_run):
    """結果をJSONとEXPERIMENTS.mdに保存する。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # JSON保存
    json_path = os.path.join(base_dir, "multi_market_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {json_path}")

    # EXPERIMENTS.md追記
    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        return

    exp_path = os.path.join(base_dir, "EXPERIMENTS.md")
    with open(exp_path, "a", encoding="utf-8") as f:
        f.write(f"\n### 実験: マルチマーケットバックテスト（{datetime.now().strftime('%Y-%m-%d %H:%M')}）\n\n")
        f.write(f"- **対象**: {', '.join(markets_run)}\n")
        f.write(f"- **パラメータ**: Optuna最適化済み\n")
        f.write(f"- **担当**: 軍師\n\n")
        f.write("| 銘柄 | 戦略 | Return | MaxDD | Sharpe | Trades |\n")
        f.write("|------|------|--------|-------|--------|--------|\n")
        for r in ok_results:
            f.write(
                f"| {r['display_name']} | {r['strategy_name']} | "
                f"{r['annual_return']:+.1f}% | {r['max_drawdown']:.1f}% | "
                f"{r['sharpe_ratio']:.2f} | {r['total_trades']} |\n"
            )
        f.write("\n")

    print(f"Results appended to: {exp_path}")


def main():
    parser = argparse.ArgumentParser(description="Multi-Market Backtest Runner")
    parser.add_argument("--market", choices=list(MARKETS.keys()) + ["all"], default="all",
                        help="Market to test (default: all)")
    parser.add_argument("--period", default="2y", help="Period (default: 2y)")
    args = parser.parse_args()

    markets_to_run = list(MARKETS.keys()) if args.market == "all" else [args.market]
    opt_params = load_optimized_params()

    if opt_params:
        print("Using optimized parameters from optimized_params.json")
    else:
        print("WARNING: optimized_params.json not found. Using default parameters.")

    results = run_multi_market(markets_to_run, args.period, opt_params)

    print_results_table(results)
    print_strategy_ranking(results)
    save_results(results, markets_to_run)


if __name__ == "__main__":
    main()

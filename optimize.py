"""
Optunaパラメータ最適化スクリプト

全7戦略のパラメータをOptunaで最適化し、最適パラメータでの
バックテスト結果を比較する。

使い方:
    python optimize.py                          # 全戦略を最適化
    python optimize.py --strategy sma           # SMAのみ
    python optimize.py --trials 200             # 試行回数変更
    python optimize.py --source ccxt --period 90d  # CCXTデータ使用
    python optimize.py --walk-forward           # ウォークフォワード検証

目的関数: シャープレシオの最大化
過学習防止: ウォークフォワード検証（--walk-forward）
"""

import argparse
import json
import logging
import os
from datetime import datetime

import optuna
import pandas as pd

from engine import BacktestEngine, BacktestConfig, YFinanceFetcher, CCXTFetcher
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.rsi_reversion import RSIMeanReversionStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy
from strategies.order_block import OrderBlockStrategy

# Optunaのログを抑制（進捗バーのみ表示）
optuna.logging.set_verbosity(optuna.logging.WARNING)
logger = logging.getLogger("auto-trade.optimize")


# ==============================================================
# 各戦略のパラメータ探索空間を定義
# ==============================================================

def suggest_sma(trial):
    return {
        "sma_short": trial.suggest_int("sma_short", 3, 20),
        "sma_long": trial.suggest_int("sma_long", 15, 60),
    }

def suggest_rsi(trial):
    return {
        "rsi_period": trial.suggest_int("rsi_period", 7, 28),
        "rsi_oversold": trial.suggest_int("rsi_oversold", 15, 40),
        "rsi_overbought": trial.suggest_int("rsi_overbought", 60, 85),
    }

def suggest_bb_rsi(trial):
    return {
        "bb_period": trial.suggest_int("bb_period", 10, 30),
        "bb_std": trial.suggest_float("bb_std", 1.5, 3.0, step=0.25),
        "rsi_period": trial.suggest_int("rsi_period", 7, 28),
        "rsi_oversold": trial.suggest_int("rsi_oversold", 15, 40),
        "rsi_overbought": trial.suggest_int("rsi_overbought", 60, 85),
    }

def suggest_monthly(trial):
    return {
        "entry_days": trial.suggest_int("entry_days", 1, 7),
        "volume_ma_period": trial.suggest_int("volume_ma_period", 10, 40),
        "volume_threshold": trial.suggest_float("volume_threshold", 1.0, 3.0, step=0.25),
    }

def suggest_volume_div(trial):
    return {
        "mfi_period": trial.suggest_int("mfi_period", 7, 28),
        "ema_period": trial.suggest_int("ema_period", 100, 300, step=50),
        "vo_short": trial.suggest_int("vo_short", 3, 10),
        "vo_long": trial.suggest_int("vo_long", 8, 20),
        "swing_lookback": trial.suggest_int("swing_lookback", 3, 10),
        "divergence_window": trial.suggest_int("divergence_window", 20, 80, step=10),
    }

def suggest_momentum_pb(trial):
    return {
        "ema_fast": trial.suggest_int("ema_fast", 5, 15),
        "ema_mid": trial.suggest_int("ema_mid", 15, 30),
        "ema_slow": trial.suggest_int("ema_slow", 100, 300, step=50),
        "pullback_tolerance": trial.suggest_float("pullback_tolerance", 0.001, 0.005, step=0.001),
        "vol_increase_lookback": trial.suggest_int("vol_increase_lookback", 2, 6),
    }

def suggest_order_block(trial):
    return {
        "ob_max_age": trial.suggest_int("ob_max_age", 10, 50, step=5),
        "ob_max_zones": trial.suggest_int("ob_max_zones", 3, 10),
        "swing_lookback": trial.suggest_int("swing_lookback", 5, 20),
    }


# 戦略名 → (クラス, パラメータ提案関数) のマッピング
STRATEGY_MAP = {
    "sma": ("SMA Crossover", SMACrossoverStrategy, suggest_sma),
    "rsi": ("RSI Reversion", RSIMeanReversionStrategy, suggest_rsi),
    "bb_rsi": ("BB+RSI Combo", BBRSIComboStrategy, suggest_bb_rsi),
    "monthly": ("Monthly Momentum", MonthlyMomentumStrategy, suggest_monthly),
    "vol_div": ("Volume Divergence", VolumeDivergenceStrategy, suggest_volume_div),
    "mom_pb": ("Momentum Pullback", MomentumPullbackStrategy, suggest_momentum_pb),
    "order_block": ("Order Block", OrderBlockStrategy, suggest_order_block),
}


# ==============================================================
# 最適化ロジック
# ==============================================================

def create_objective(strategy_cls, suggest_fn, data, engine, use_walk_forward):
    """Optunaの目的関数を生成する。"""

    def objective(trial):
        params = suggest_fn(trial)

        # パラメータの整合性チェック（SMA: short < long）
        if "sma_short" in params and "sma_long" in params:
            if params["sma_short"] >= params["sma_long"]:
                return float("-inf")

        # VO: short < long
        if "vo_short" in params and "vo_long" in params:
            if params["vo_short"] >= params["vo_long"]:
                return float("-inf")

        # EMA: fast < mid < slow
        if "ema_fast" in params and "ema_mid" in params:
            if params["ema_fast"] >= params["ema_mid"]:
                return float("-inf")

        try:
            strategy = strategy_cls(params=params)

            if use_walk_forward:
                results = engine.walk_forward(strategy, data, verbose=False)
                if not results:
                    return float("-inf")
                # ウォークフォワードの平均シャープレシオ
                avg_sharpe = sum(r.sharpe_ratio for r in results) / len(results)
                return avg_sharpe
            else:
                result = engine.run(strategy, data, verbose=False)
                # トレード数が少なすぎる場合はペナルティ
                if result.total_trades < 5:
                    return float("-inf")
                return result.sharpe_ratio
        except Exception as e:
            logger.debug("Trial failed: %s", e)
            return float("-inf")

    return objective


def optimize_strategy(
    key, data, engine, n_trials, use_walk_forward
):
    """1つの戦略を最適化する。"""
    display_name, strategy_cls, suggest_fn = STRATEGY_MAP[key]
    print(f"\n{'='*60}")
    print(f"  Optimizing: {display_name} ({n_trials} trials)")
    print(f"{'='*60}")

    study = optuna.create_study(direction="maximize")
    objective = create_objective(
        strategy_cls, suggest_fn, data, engine, use_walk_forward
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_trial
    print(f"\n  Best Sharpe: {best.value:.4f}")
    print(f"  Best Params: {json.dumps(best.params, indent=4)}")

    # 最適パラメータでバックテスト
    best_strategy = strategy_cls(params=best.params)
    best_result = engine.run(best_strategy, data, verbose=True)

    # デフォルトパラメータでのバックテスト（比較用）
    default_strategy = strategy_cls()
    default_result = engine.run(default_strategy, data, verbose=False)

    return {
        "name": display_name,
        "key": key,
        "best_params": best.params,
        "best_sharpe": best.value,
        "best_result": best_result,
        "default_result": default_result,
        "n_trials": n_trials,
    }


def print_comparison_table(results):
    """最適化前後の比較表を出力する。"""
    print(f"\n{'='*80}")
    print("  OPTIMIZATION RESULTS: Before vs After")
    print(f"{'='*80}")
    print(f"{'Strategy':<22} {'Metric':<10} {'Before':>10} {'After':>10} {'Change':>10}")
    print(f"{'-'*62}")

    for r in results:
        d = r["default_result"]
        b = r["best_result"]

        print(f"{r['name']:<22} {'Return%':<10} {d.annual_return:>+9.1f}% {b.annual_return:>+9.1f}% {b.annual_return - d.annual_return:>+9.1f}%")
        print(f"{'':<22} {'MaxDD%':<10} {d.max_drawdown:>9.1f}% {b.max_drawdown:>9.1f}% {b.max_drawdown - d.max_drawdown:>+9.1f}%")
        print(f"{'':<22} {'Sharpe':<10} {d.sharpe_ratio:>10.2f} {b.sharpe_ratio:>10.2f} {b.sharpe_ratio - d.sharpe_ratio:>+10.2f}")
        print(f"{'':<22} {'Trades':<10} {d.total_trades:>10d} {b.total_trades:>10d} {b.total_trades - d.total_trades:>+10d}")
        print(f"{'-'*62}")


def save_results_to_experiments(results, data_info):
    """結果をEXPERIMENTS.mdに追記する。"""
    exp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EXPERIMENTS.md")

    lines = []
    lines.append(f"\n### 実験: Optunaパラメータ最適化（{datetime.now().strftime('%Y-%m-%d %H:%M')}）\n")
    lines.append(f"- **データ**: {data_info}\n")
    lines.append(f"- **目的関数**: シャープレシオ最大化\n")
    lines.append(f"- **担当**: 軍師\n\n")
    lines.append("| 戦略 | 指標 | 最適化前 | 最適化後 | 改善幅 |\n")
    lines.append("|------|------|---------|---------|--------|\n")

    for r in results:
        d = r["default_result"]
        b = r["best_result"]
        lines.append(
            f"| {r['name']} | Return | {d.annual_return:+.1f}% | {b.annual_return:+.1f}% | {b.annual_return - d.annual_return:+.1f}% |\n"
        )
        lines.append(
            f"| | MaxDD | {d.max_drawdown:.1f}% | {b.max_drawdown:.1f}% | {b.max_drawdown - d.max_drawdown:+.1f}% |\n"
        )
        lines.append(
            f"| | Sharpe | {d.sharpe_ratio:.2f} | {b.sharpe_ratio:.2f} | {b.sharpe_ratio - d.sharpe_ratio:+.2f} |\n"
        )
        lines.append(
            f"| | Trades | {d.total_trades} | {b.total_trades} | {b.total_trades - d.total_trades:+d} |\n"
        )

    lines.append("\n**最適パラメータ:**\n\n")
    lines.append("```json\n")
    for r in results:
        lines.append(f"// {r['name']}\n")
        lines.append(json.dumps(r["best_params"], indent=2) + "\n\n")
    lines.append("```\n")

    with open(exp_path, "a", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"\nResults appended to: {exp_path}")


def main():
    parser = argparse.ArgumentParser(description="Optuna Parameter Optimization for Auto-Trade Strategies")
    parser.add_argument("--strategy", choices=list(STRATEGY_MAP.keys()) + ["all"], default="all",
                        help="Strategy to optimize (default: all)")
    parser.add_argument("--trials", type=int, default=100,
                        help="Number of Optuna trials per strategy (default: 100)")
    parser.add_argument("--source", choices=["yfinance", "ccxt"], default="yfinance",
                        help="Data source (default: yfinance)")
    parser.add_argument("--symbol", default="BTC-USD",
                        help="Symbol (default: BTC-USD)")
    parser.add_argument("--period", default="2y",
                        help="Period (default: 2y)")
    parser.add_argument("--exchange", default="bybit",
                        help="Exchange for ccxt (default: bybit)")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Use walk-forward validation (prevents overfitting)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # データ取得
    print("Loading data...")
    if args.source == "ccxt":
        symbol = args.symbol if "/" in args.symbol else "BTC/USDT"
        fetcher = CCXTFetcher(exchange=args.exchange)
        data = fetcher.fetch(symbol, period=args.period)
        data_info = f"{symbol} ({args.period}, {args.exchange})"
    else:
        fetcher = YFinanceFetcher()
        data = fetcher.fetch(args.symbol, period=args.period)
        data_info = f"{args.symbol} ({args.period}, yfinance)"

    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))

    # 最適化実行
    strategies_to_optimize = (
        list(STRATEGY_MAP.keys()) if args.strategy == "all"
        else [args.strategy]
    )

    results = []
    for key in strategies_to_optimize:
        result = optimize_strategy(
            key, data, engine, args.trials, args.walk_forward
        )
        results.append(result)

    # 比較表を出力
    print_comparison_table(results)

    # EXPERIMENTS.mdに記録
    save_results_to_experiments(results, data_info)

    # 最適パラメータをJSONファイルに保存
    params_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimized_params.json")
    params_dict = {r["key"]: r["best_params"] for r in results}
    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(params_dict, f, indent=2, ensure_ascii=False)
    print(f"Optimized params saved to: {params_path}")


if __name__ == "__main__":
    main()

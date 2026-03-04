"""
全戦略の一括バックテストスクリプト

使い方:
    python run_backtest.py              # YFinanceでBTC-USD（デフォルト）
    python run_backtest.py --source ccxt --symbol BTC/USDT  # CCXTで取得
"""

import os
import argparse
import pandas as pd
import numpy as np

from engine import BacktestEngine, BacktestConfig, YFinanceFetcher, CCXTFetcher
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.rsi_reversion import RSIMeanReversionStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy
from strategies.order_block import OrderBlockStrategy


def main():
    parser = argparse.ArgumentParser(description="Auto-trade backtest runner")
    parser.add_argument("--source", choices=["yfinance", "ccxt"], default="yfinance",
                        help="Data source (default: yfinance)")
    parser.add_argument("--symbol", default="BTC-USD",
                        help="Symbol (default: BTC-USD for yfinance, BTC/USDT for ccxt)")
    parser.add_argument("--period", default="1y", help="Period (default: 1y)")
    parser.add_argument("--exchange", default="bybit",
                        help="Exchange for ccxt (default: bybit)")
    args = parser.parse_args()

    # データ取得
    if args.source == "ccxt":
        symbol = args.symbol if "/" in args.symbol else "BTC/USDT"
        fetcher = CCXTFetcher(exchange=args.exchange)
        data = fetcher.fetch(symbol, period=args.period)
    else:
        fetcher = YFinanceFetcher()
        data = fetcher.fetch(args.symbol, period=args.period)

    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))

    # 全戦略を実行
    strategies = [
        ("SMA Crossover", SMACrossoverStrategy()),
        ("RSI Reversion", RSIMeanReversionStrategy()),
        ("BB + RSI Combo", BBRSIComboStrategy()),
        ("Monthly Momentum", MonthlyMomentumStrategy()),
        ("Volume Divergence", VolumeDivergenceStrategy()),
        ("Momentum Pullback", MomentumPullbackStrategy()),
        ("Order Block", OrderBlockStrategy()),
    ]

    results = []
    for name, strat in strategies:
        print(f"\n--- Running {name} Backtest ---")
        res = engine.run(strat, data)
        results.append((name, res))

    # 結果の保存
    results_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.txt")
    with open(results_path, "w") as f:
        for name, res in results:
            f.write(f"{name} Summary:\n")
            f.write(res.summary() + "\n\n")

    print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    main()

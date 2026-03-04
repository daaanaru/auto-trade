"""
短期足バックテスト: 全7戦略を日足・1時間足・5分足で比較検証する

Task #26: YouTube動画で推奨されていた短期足での戦略検証
- ccxtのCCXTFetcherでBybit BTC/USDTのデータを取得
- 全7戦略を1時間足でバックテスト
- Volume Divergence, Momentum Pullbackは5分足でもバックテスト
- 日足・1時間足・5分足の結果比較表を作成

使い方:
    cd 50_ラボ/auto-trade/
    python run_timeframe_backtest.py
"""

import os
import sys
import json
import traceback
import pandas as pd
import numpy as np
from datetime import datetime

from engine import BacktestEngine, BacktestConfig, CCXTFetcher
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.rsi_reversion import RSIMeanReversionStrategy
from strategies.bb_rsi_combo import BBRSIComboStrategy
from strategies.monthly_momentum import MonthlyMomentumStrategy
from strategies.volume_divergence import VolumeDivergenceStrategy
from strategies.momentum_pullback import MomentumPullbackStrategy
from strategies.order_block import OrderBlockStrategy


def fetch_data(interval: str, period: str = "90d") -> pd.DataFrame:
    """Bybit BTC/USDTのデータを取得する。"""
    fetcher = CCXTFetcher(exchange="bybit")
    data = fetcher.fetch("BTC/USDT", period=period, interval=interval)
    return data


def run_all_strategies(engine, data, timeframe_label):
    """全7戦略をバックテストし、結果を返す。"""
    strategies = [
        ("SMA_Crossover", SMACrossoverStrategy()),
        ("RSI_Reversion", RSIMeanReversionStrategy()),
        ("BB_RSI_Combo", BBRSIComboStrategy()),
        ("Monthly_Momentum", MonthlyMomentumStrategy()),
        ("Volume_Divergence", VolumeDivergenceStrategy()),
        ("Momentum_Pullback", MomentumPullbackStrategy()),
        ("Order_Block", OrderBlockStrategy()),
    ]

    results = {}
    for name, strat in strategies:
        print(f"\n  [{timeframe_label}] {name}...")
        try:
            res = engine.run(strat, data, verbose=False)
            results[name] = {
                "annual_return": round(res.annual_return, 2),
                "max_drawdown": round(res.max_drawdown, 2),
                "sharpe_ratio": round(res.sharpe_ratio, 2),
                "win_rate": round(res.win_rate, 1),
                "total_trades": res.total_trades,
                "period": res.period,
                "status": "OK",
            }
            print(f"    -> Return: {res.annual_return:+.1f}%, Sharpe: {res.sharpe_ratio:.2f}, "
                  f"Trades: {res.total_trades}")
        except Exception as e:
            results[name] = {
                "annual_return": None,
                "max_drawdown": None,
                "sharpe_ratio": None,
                "win_rate": None,
                "total_trades": 0,
                "period": "",
                "status": f"ERROR: {str(e)[:80]}",
            }
            print(f"    -> ERROR: {e}")
            traceback.print_exc()

    return results


def run_selected_strategies(engine, data, timeframe_label, strategy_names):
    """指定した戦略のみバックテストする（5分足用）。"""
    strategy_map = {
        "Volume_Divergence": VolumeDivergenceStrategy(),
        "Momentum_Pullback": MomentumPullbackStrategy(),
    }

    results = {}
    for name in strategy_names:
        if name not in strategy_map:
            continue
        strat = strategy_map[name]
        print(f"\n  [{timeframe_label}] {name}...")
        try:
            res = engine.run(strat, data, verbose=False)
            results[name] = {
                "annual_return": round(res.annual_return, 2),
                "max_drawdown": round(res.max_drawdown, 2),
                "sharpe_ratio": round(res.sharpe_ratio, 2),
                "win_rate": round(res.win_rate, 1),
                "total_trades": res.total_trades,
                "period": res.period,
                "status": "OK",
            }
            print(f"    -> Return: {res.annual_return:+.1f}%, Sharpe: {res.sharpe_ratio:.2f}, "
                  f"Trades: {res.total_trades}")
        except Exception as e:
            results[name] = {
                "annual_return": None,
                "max_drawdown": None,
                "sharpe_ratio": None,
                "win_rate": None,
                "total_trades": 0,
                "period": "",
                "status": f"ERROR: {str(e)[:80]}",
            }
            print(f"    -> ERROR: {e}")
            traceback.print_exc()

    return results


def format_comparison_table(all_results):
    """結果比較表をテキストで生成する。"""
    lines = []
    lines.append("\n" + "=" * 100)
    lines.append("  短期足バックテスト 結果比較表")
    lines.append("  BTC/USDT (Bybit) | 初期資金: 100万円 | 手数料0.1% + スリッページ0.05%")
    lines.append("=" * 100)

    # 全戦略の名前を収集
    all_strategy_names = set()
    for tf, results in all_results.items():
        all_strategy_names.update(results.keys())
    all_strategy_names = sorted(all_strategy_names)

    # ヘッダー
    timeframes = list(all_results.keys())
    header = f"{'Strategy':<25}"
    for tf in timeframes:
        header += f"| {'Return':>8} {'Sharpe':>7} {'DD':>7} {'WR':>6} {'Trades':>7} "
    lines.append("")
    lines.append(f"{'':25}" + "".join(f"| {'--- ' + tf + ' ---':^37}" for tf in timeframes))
    lines.append(header)
    lines.append("-" * (25 + 39 * len(timeframes)))

    for name in all_strategy_names:
        row = f"{name:<25}"
        for tf in timeframes:
            r = all_results[tf].get(name)
            if r is None:
                row += f"|{'-- N/A --':^38}"
            elif r["status"] != "OK":
                row += f"|{'ERROR':^38}"
            else:
                ret = f"{r['annual_return']:+.1f}%"
                sh = f"{r['sharpe_ratio']:.2f}"
                dd = f"{r['max_drawdown']:.1f}%"
                wr = f"{r['win_rate']:.0f}%"
                tr = str(r['total_trades'])
                row += f"| {ret:>8} {sh:>7} {dd:>7} {wr:>6} {tr:>7} "
        lines.append(row)

    lines.append("=" * (25 + 39 * len(timeframes)))
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("  短期足バックテスト検証 (Task #26)")
    print(f"  実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # BacktestEngine設定
    # 短期足は手数料率が同じでもトレード頻度が高いため影響が大きい
    config = BacktestConfig(initial_capital=1_000_000)
    engine = BacktestEngine(config)

    all_results = {}

    # ----------------------------------------------------------
    # 1. 日足（1d）: 全7戦略 — 90日分
    # ----------------------------------------------------------
    print("\n" + "=" * 40)
    print("  Phase 1: 日足（1d）全7戦略")
    print("=" * 40)
    try:
        data_1d = fetch_data("1d", period="90d")
        print(f"  データ取得: {len(data_1d)} bars")
        results_1d = run_all_strategies(engine, data_1d, "日足")
        all_results["日足(1d)"] = results_1d
    except Exception as e:
        print(f"  日足データ取得エラー: {e}")
        traceback.print_exc()
        all_results["日足(1d)"] = {}

    # ----------------------------------------------------------
    # 2. 1時間足（1h）: 全7戦略 — 90日分
    # ----------------------------------------------------------
    print("\n" + "=" * 40)
    print("  Phase 2: 1時間足（1h）全7戦略")
    print("=" * 40)
    try:
        data_1h = fetch_data("1h", period="90d")
        print(f"  データ取得: {len(data_1h)} bars")
        results_1h = run_all_strategies(engine, data_1h, "1時間足")
        all_results["1時間足(1h)"] = results_1h
    except Exception as e:
        print(f"  1時間足データ取得エラー: {e}")
        traceback.print_exc()
        all_results["1時間足(1h)"] = {}

    # ----------------------------------------------------------
    # 3. 5分足（5m）: Volume Divergence & Momentum Pullback — 30日分
    #    （5分足は90日だとデータが膨大なので30日に制限）
    # ----------------------------------------------------------
    print("\n" + "=" * 40)
    print("  Phase 3: 5分足（5m）選択戦略")
    print("  (Volume Divergence, Momentum Pullback)")
    print("=" * 40)
    try:
        data_5m = fetch_data("5m", period="90d")
        print(f"  データ取得: {len(data_5m)} bars")
        results_5m = run_selected_strategies(
            engine, data_5m, "5分足",
            ["Volume_Divergence", "Momentum_Pullback"]
        )
        all_results["5分足(5m)"] = results_5m
    except Exception as e:
        print(f"  5分足データ取得エラー: {e}")
        traceback.print_exc()
        all_results["5分足(5m)"] = {}

    # ----------------------------------------------------------
    # 結果表示
    # ----------------------------------------------------------
    table = format_comparison_table(all_results)
    print(table)

    # ----------------------------------------------------------
    # 結果をJSONで保存
    # ----------------------------------------------------------
    output_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(output_dir, "timeframe_backtest_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n結果JSON保存先: {json_path}")

    # ----------------------------------------------------------
    # 結果をテキストで保存
    # ----------------------------------------------------------
    txt_path = os.path.join(output_dir, "timeframe_backtest_results.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"短期足バックテスト検証 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"BTC/USDT (Bybit) | 初期資金: 100万円\n\n")
        f.write(table)
    print(f"結果TXT保存先: {txt_path}")


if __name__ == "__main__":
    main()

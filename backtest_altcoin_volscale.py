#!/usr/bin/env python3
"""
アルトコイン VolScale戦略 バックテスト + WF検証

対象: XRP-JPY, XLM-JPY, ETH-JPY, BTC-JPY（比較用）
目的: 無検証で稼働中のアルトコインVolScale戦略の有効性をデータで確認する

使い方:
    python3 backtest_altcoin_volscale.py
"""

import sys
import os
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from engine import BacktestEngine, BacktestConfig, YFinanceFetcher
from strategies.volscale_sma import VolScaleSMAStrategy
from plugins.strategies.base_strategy import BacktestResult


def walk_forward_with_warmup(
    engine: BacktestEngine,
    strategy: VolScaleSMAStrategy,
    data: pd.DataFrame,
    train_months: int = 12,
    test_months: int = 3,
    warmup_days: int = 200,
) -> list[dict]:
    """VolScale用WF検証。テスト期間にもウォームアップデータを含めてシグナル生成する。

    エンジンのwalk_forward()は test_data だけを渡すため、
    ref_w=180日のウォームアップが足りずシグナルが全0になる問題を修正。

    方式: 全データでシグナル生成 → テスト期間のみで損益を評価。
    """
    results = []
    start = data.index[0]
    end = data.index[-1]

    # 全データでシグナル一括生成（ルックアヘッドバイアスなし: SMAは過去データのみ使用）
    signals = strategy.generate_signals(data)
    signals = signals.shift(1).fillna(0)  # 翌日始値で執行

    close = data["close"]
    returns = close.pct_change().fillna(0)

    current = start + pd.DateOffset(months=train_months)
    fold = 1

    while current + pd.DateOffset(months=test_months) <= end:
        test_end = current + pd.DateOffset(months=test_months)

        # テスト期間のスライス
        mask = (data.index >= current) & (data.index < test_end)
        test_signals = signals[mask]
        test_returns = returns[mask]

        if len(test_signals) == 0:
            current = test_end
            fold += 1
            continue

        # 損益計算
        position = test_signals.copy()
        trades = position.diff().abs()
        costs = trades * (engine.config.commission_rate + engine.config.slippage_rate)
        strategy_returns = (position * test_returns) - costs
        equity_curve = (1 + strategy_returns).cumprod() * engine.config.initial_capital

        # Buy & Hold
        bh_returns = test_returns
        bh_equity = (1 + bh_returns).cumprod() * engine.config.initial_capital

        # 統計
        total_ret = (equity_curve.iloc[-1] / engine.config.initial_capital - 1) * 100
        days = len(test_signals)
        annual_return = ((1 + total_ret / 100) ** (365 / days) - 1) * 100 if days > 0 else 0

        # Sharpe
        if strategy_returns.std() > 0:
            sharpe = (strategy_returns.mean() / strategy_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # MaxDD
        peak = equity_curve.cummax()
        dd = (equity_curve - peak) / peak
        max_dd = dd.min() * 100

        # B&H
        bh_total = (bh_equity.iloc[-1] / engine.config.initial_capital - 1) * 100
        bh_annual = ((1 + bh_total / 100) ** (365 / days) - 1) * 100 if days > 0 else 0
        if bh_returns.std() > 0:
            bh_sharpe = (bh_returns.mean() / bh_returns.std()) * np.sqrt(252)
        else:
            bh_sharpe = 0.0

        # トレード数
        trade_count = int(trades.sum())

        win = "勝ち" if sharpe > bh_sharpe else "負け"

        print(f"  Fold {fold}: {current.date()} ～ {test_end.date()} | "
              f"年率 {annual_return:+.1f}% | Sharpe {sharpe:.2f} | "
              f"B&H Sharpe {bh_sharpe:.2f} | {win}")

        results.append({
            "fold": fold,
            "period": f"{current.date()} ～ {test_end.date()}",
            "annual_return": annual_return,
            "sharpe": sharpe,
            "max_dd": max_dd,
            "bh_sharpe": bh_sharpe,
            "bh_annual": bh_annual,
            "trades": trade_count,
            "win": sharpe > bh_sharpe,
        })

        current = test_end
        fold += 1

    return results


def run_backtest(symbol: str, name: str, fetcher, engine, strategy):
    """1銘柄のバックテスト + WF検証を実行"""
    print(f"\n{'='*60}")
    print(f"  {name} ({symbol})")
    print(f"{'='*60}")

    # データ取得（最大期間）
    try:
        data = fetcher.fetch(symbol=symbol, period="2y", interval="1d")
    except Exception as e:
        print(f"  [ERROR] データ取得失敗: {e}")
        return None

    if data is None or len(data) < 200:
        print(f"  [SKIP] データ不足（{len(data) if data is not None else 0}行、200行必要）")
        return None

    print(f"  データ期間: {data.index[0].date()} ～ {data.index[-1].date()} ({len(data)}行)")

    # 全期間バックテスト
    result_full = engine.run(strategy, data, verbose=True)

    # Buy & Hold比較
    bh_return = ((data["close"].iloc[-1] / data["close"].iloc[0]) - 1) * 100
    years = len(data) / 365
    bh_annual = ((1 + bh_return / 100) ** (1 / years) - 1) * 100 if years > 0 else 0

    print(f"\n  Buy & Hold比較:")
    print(f"     Buy&Hold年率: {bh_annual:+.1f}%")
    print(f"     VolScale年率: {result_full.annual_return:+.1f}%")
    print(f"     超過リターン: {result_full.annual_return - bh_annual:+.1f}%")

    # WF検証（ウォームアップ付き）
    print(f"\n  ウォークフォワード検証（ウォームアップ200日付き）...")
    wf_results = walk_forward_with_warmup(engine, strategy, data, train_months=12, test_months=3)

    if wf_results:
        avg_sharpe = np.mean([r["sharpe"] for r in wf_results])
        avg_return = np.mean([r["annual_return"] for r in wf_results])
        wins = sum(1 for r in wf_results if r["win"])
        print(f"\n  WF集計 ({len(wf_results)}フォールド): "
              f"平均Sharpe {avg_sharpe:.2f} | 平均年率 {avg_return:+.1f}% | "
              f"vs B&H勝率 {wins}/{len(wf_results)}")
    else:
        avg_sharpe = None
        avg_return = None

    return {
        "symbol": symbol,
        "name": name,
        "data_rows": len(data),
        "full_annual_return": result_full.annual_return,
        "full_sharpe": result_full.sharpe_ratio,
        "full_max_dd": result_full.max_drawdown,
        "full_win_rate": result_full.win_rate,
        "full_trades": result_full.total_trades,
        "bh_annual": bh_annual,
        "wf_folds": len(wf_results) if wf_results else 0,
        "wf_avg_sharpe": avg_sharpe,
        "wf_avg_return": avg_return,
        "wf_results": wf_results,
    }


def main():
    # 設定
    fetcher = YFinanceFetcher()
    engine = BacktestEngine(BacktestConfig(
        initial_capital=300_000,
        commission_rate=0.001,
        slippage_rate=0.0005,
    ))
    strategy = VolScaleSMAStrategy()

    # 対象銘柄
    targets = [
        ("BTC-JPY", "ビットコイン（基準）"),
        ("ETH-JPY", "イーサリアム"),
        ("XRP-JPY", "リップル"),
        ("XLM-JPY", "ステラルーメン"),
    ]

    results = []
    for symbol, name in targets:
        r = run_backtest(symbol, name, fetcher, engine, strategy)
        if r:
            results.append(r)

    # === サマリー ===
    print(f"\n{'='*60}")
    print(f"  VolScale SMA アルトコインバックテスト サマリー")
    print(f"{'='*60}")
    print(f"\n{'銘柄':<14} {'年率':>8} {'Sharpe':>8} {'MDD':>8} {'WF Sharpe':>10} {'WF年率':>8} {'判定':>6}")
    print("-" * 72)

    for r in results:
        wf_sharpe = f"{r['wf_avg_sharpe']:.2f}" if r['wf_avg_sharpe'] is not None else "N/A"
        wf_return = f"{r['wf_avg_return']:+.1f}%" if r['wf_avg_return'] is not None else "N/A"

        # 判定基準
        if r['wf_avg_sharpe'] is not None and r['wf_avg_sharpe'] >= 0.3 and r['wf_avg_return'] is not None and r['wf_avg_return'] > 0:
            verdict = "合格"
        elif r['wf_avg_sharpe'] is not None and r['wf_avg_sharpe'] >= 0.0:
            verdict = "保留"
        else:
            verdict = "不合格"

        print(f"{r['name']:<14} {r['full_annual_return']:>+7.1f}% {r['full_sharpe']:>7.2f} {r['full_max_dd']:>+7.1f}% {wf_sharpe:>10} {wf_return:>8} {verdict:>6}")

    print()

    # レポートファイル出力
    report_path = os.path.join(BASE_DIR, "research", "20260316_altcoin-volscale-wf-result.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w") as f:
        f.write("# アルトコイン VolScale SMA バックテスト結果\n\n")
        f.write("**実行日**: 2026-03-16\n")
        f.write("**目的**: 無検証で稼働中のアルトコインVolScaleの有効性確認\n")
        f.write("**パラメータ**: base_n=50, vol_w=20, ref_w=180（BTC検証済みの固定値）\n")
        f.write("**WF方式**: 全データでシグナル生成→テスト期間のみ損益評価（ウォームアップ問題を回避）\n\n")

        f.write("## 全期間バックテスト（2年間）\n\n")
        f.write("| 銘柄 | 年率リターン | Sharpe | MaxDD | 勝率 | トレード数 | B&H年率 |\n")
        f.write("|------|-------------|--------|-------|------|-----------|--------|\n")
        for r in results:
            f.write(f"| {r['name']} | {r['full_annual_return']:+.1f}% | {r['full_sharpe']:.2f} | {r['full_max_dd']:+.1f}% | {r['full_win_rate']:.1f}% | {r['full_trades']} | {r['bh_annual']:+.1f}% |\n")

        f.write("\n## ウォークフォワード検証（Train 12ヶ月 / Test 3ヶ月）\n\n")
        for r in results:
            f.write(f"### {r['name']} ({r['symbol']})\n\n")
            if r['wf_results']:
                f.write("| Fold | テスト期間 | 年率 | Sharpe | B&H Sharpe | 勝敗 |\n")
                f.write("|------|----------|------|--------|-----------|------|\n")
                for wf in r['wf_results']:
                    win_mark = "勝ち" if wf['win'] else "負け"
                    f.write(f"| {wf['fold']} | {wf['period']} | {wf['annual_return']:+.1f}% | {wf['sharpe']:.2f} | {wf['bh_sharpe']:.2f} | {win_mark} |\n")
                f.write(f"\n**WF平均**: Sharpe {r['wf_avg_sharpe']:.2f} / 年率 {r['wf_avg_return']:+.1f}%\n\n")
            else:
                f.write("（データ不足でWF検証不可）\n\n")

        f.write("## サマリー\n\n")
        f.write("| 銘柄 | WF平均Sharpe | WF平均年率 | 判定 |\n")
        f.write("|------|-------------|----------|------|\n")
        for r in results:
            wf_s = f"{r['wf_avg_sharpe']:.2f}" if r['wf_avg_sharpe'] is not None else "N/A"
            wf_r = f"{r['wf_avg_return']:+.1f}%" if r['wf_avg_return'] is not None else "N/A"
            if r['wf_avg_sharpe'] is not None and r['wf_avg_sharpe'] >= 0.3 and r['wf_avg_return'] is not None and r['wf_avg_return'] > 0:
                v = "合格"
            elif r['wf_avg_sharpe'] is not None and r['wf_avg_sharpe'] >= 0.0:
                v = "保留"
            else:
                v = "不合格"
            f.write(f"| {r['name']} | {wf_s} | {wf_r} | {v} |\n")

        f.write("\n## 結論\n\n")
        f.write("（バックテスト結果に基づき自動記入）\n")

    print(f"レポート出力: {report_path}")


if __name__ == "__main__":
    main()

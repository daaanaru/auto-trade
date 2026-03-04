#!/usr/bin/env python3
"""
日本株全自動スクリーニング＋ウォークフォワード検証パイプライン

日経225銘柄をスキャンし、Monthly Momentum戦略が有効な銘柄を自動発掘する。
Mac mini M4 16GBで一晩回せる設計。

Phase 1: 銘柄ユニバース取得（nikkei225_tickers.json）
Phase 2: 一括バックテスト（Sharpe > 0 & Return > 5% を候補抽出）
Phase 3: ウォークフォワード検証（候補銘柄の過学習チェック）
Phase 4: watchlist.json自動更新（signal_monitor.py連携）

使い方:
    python jp_stock_screener.py                    # 全フェーズ実行
    python jp_stock_screener.py --phase 2          # Phase 2からのみ
    python jp_stock_screener.py --phase 3          # Phase 3からのみ（要: screening_results.json）
    python jp_stock_screener.py --top 20           # 上位20銘柄のみWF検証
    python jp_stock_screener.py --dry-run          # WF合格銘柄の表示のみ（watchlist更新なし）

cron設定例（毎週日曜深夜に全銘柄スキャン）:
    0 0 * * 0 cd /path/to/auto-trade && python3 jp_stock_screener.py >> screening_log.txt 2>&1
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

# auto-trade ディレクトリをパスに追加
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from engine import BacktestEngine, BacktestConfig
from strategies.monthly_momentum import MonthlyMomentumStrategy


# ==============================================================
# 設定
# ==============================================================

# バックテスト候補の閾値
MIN_SHARPE = 0.0
MIN_RETURN = 5.0  # %
MIN_TRADES = 3

# ウォークフォワード合格基準
WF_MIN_AVG_SHARPE = 0.0
WF_MIN_POSITIVE_FOLDS = 3  # 4フォールド中3以上

# データ取得のレート制限（秒）
FETCH_DELAY = 0.5


# ==============================================================
# Phase 1: 銘柄ユニバース取得
# ==============================================================

def load_tickers() -> list:
    """nikkei225_tickers.json から銘柄リストを読み込む。"""
    tickers_path = os.path.join(BASE_DIR, "nikkei225_tickers.json")
    if not os.path.exists(tickers_path):
        print(f"ERROR: {tickers_path} が見つかりません。")
        sys.exit(1)
    with open(tickers_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["tickers"]


def fetch_stock_data(symbol: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """yfinance から日足データを取得する。エラー時はNoneを返す。"""
    try:
        df = yf.download(symbol, period=period, interval="1d", progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.dropna()
        if len(df) < 100:  # 最低100日分必要
            return None
        return df
    except Exception:
        return None


# ==============================================================
# Phase 2: 一括バックテスト
# ==============================================================

def run_backtest_screening(tickers: list, params: dict, period: str = "2y") -> list:
    """全銘柄でバックテストを実行し、候補を抽出する。"""
    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))
    results = []
    total = len(tickers)

    print(f"\n{'='*60}")
    print(f"  Phase 2: 一括バックテスト ({total}銘柄)")
    print(f"{'='*60}\n")

    for i, ticker in enumerate(tickers):
        code = ticker["code"]
        name = ticker["name"]
        progress = f"[{i+1}/{total}]"

        data = fetch_stock_data(code, period=period)
        if data is None:
            print(f"  {progress} {name}({code}): SKIP (データ不足)")
            results.append({
                "code": code,
                "name": name,
                "status": "skip",
                "reason": "insufficient_data",
            })
            time.sleep(FETCH_DELAY)
            continue

        try:
            strategy = MonthlyMomentumStrategy(params=params)
            result = engine.run(strategy, data, verbose=False)

            entry = {
                "code": code,
                "name": name,
                "status": "ok",
                "sharpe": round(result.sharpe_ratio, 4),
                "annual_return": round(result.annual_return, 2),
                "max_drawdown": round(result.max_drawdown, 2),
                "win_rate": round(result.win_rate, 1),
                "total_trades": result.total_trades,
                "data_bars": len(data),
            }
            results.append(entry)

            # 候補判定
            is_candidate = (
                result.sharpe_ratio > MIN_SHARPE
                and result.annual_return > MIN_RETURN
                and result.total_trades >= MIN_TRADES
            )
            marker = " *** CANDIDATE" if is_candidate else ""
            print(
                f"  {progress} {name}({code}): "
                f"Sharpe={result.sharpe_ratio:.2f} "
                f"Return={result.annual_return:+.1f}% "
                f"Trades={result.total_trades}{marker}"
            )
        except Exception as e:
            print(f"  {progress} {name}({code}): ERROR ({e})")
            results.append({
                "code": code,
                "name": name,
                "status": "error",
                "reason": str(e),
            })

        time.sleep(FETCH_DELAY)

    return results


def filter_candidates(results: list) -> list:
    """バックテスト結果から候補銘柄を抽出する。"""
    candidates = []
    for r in results:
        if r["status"] != "ok":
            continue
        if (
            r["sharpe"] > MIN_SHARPE
            and r["annual_return"] > MIN_RETURN
            and r["total_trades"] >= MIN_TRADES
        ):
            candidates.append(r)

    # Sharpe降順でソート
    candidates.sort(key=lambda x: x["sharpe"], reverse=True)
    return candidates


# ==============================================================
# Phase 3: ウォークフォワード検証
# ==============================================================

def run_walk_forward_validation(
    candidates: list, params: dict, top_n: Optional[int] = None, period: str = "2y"
) -> list:
    """候補銘柄にウォークフォワード検証を実行する。"""
    engine = BacktestEngine(BacktestConfig(initial_capital=1000000))

    targets = candidates[:top_n] if top_n else candidates
    total = len(targets)

    print(f"\n{'='*60}")
    print(f"  Phase 3: ウォークフォワード検証 ({total}銘柄)")
    print(f"  基準: 平均Sharpe > {WF_MIN_AVG_SHARPE}, 正フォールド >= {WF_MIN_POSITIVE_FOLDS}/4")
    print(f"{'='*60}\n")

    wf_results = []

    for i, candidate in enumerate(targets):
        code = candidate["code"]
        name = candidate["name"]
        progress = f"[{i+1}/{total}]"

        data = fetch_stock_data(code, period=period)
        if data is None:
            print(f"  {progress} {name}({code}): SKIP (データ再取得失敗)")
            wf_results.append({
                **candidate,
                "wf_status": "skip",
            })
            time.sleep(FETCH_DELAY)
            continue

        try:
            strategy = MonthlyMomentumStrategy(params=params)
            fold_results = engine.walk_forward(
                strategy, data, train_months=12, test_months=3, verbose=False
            )

            if not fold_results:
                print(f"  {progress} {name}({code}): WARN (フォールド不足)")
                wf_results.append({
                    **candidate,
                    "wf_status": "warn",
                    "wf_reason": "insufficient_folds",
                    "wf_folds": 0,
                    "wf_positive_folds": 0,
                    "wf_avg_sharpe": 0.0,
                    "wf_avg_return": 0.0,
                    "wf_avg_dd": 0.0,
                    "wf_std_sharpe": 0.0,
                })
                time.sleep(FETCH_DELAY)
                continue

            sharpes = [r.sharpe_ratio for r in fold_results]
            returns = [r.annual_return for r in fold_results]
            drawdowns = [r.max_drawdown for r in fold_results]
            positive_folds = sum(1 for s in sharpes if s > 0)

            avg_sharpe = float(np.mean(sharpes))
            avg_return = float(np.mean(returns))
            avg_dd = float(np.mean(drawdowns))
            std_sharpe = float(np.std(sharpes))

            # 合格判定
            passed = (
                avg_sharpe > WF_MIN_AVG_SHARPE
                and positive_folds >= WF_MIN_POSITIVE_FOLDS
            )
            status = "PASS" if passed else "FAIL"

            wf_results.append({
                **candidate,
                "wf_status": status.lower(),
                "wf_folds": len(fold_results),
                "wf_positive_folds": positive_folds,
                "wf_avg_sharpe": round(avg_sharpe, 4),
                "wf_avg_return": round(avg_return, 2),
                "wf_avg_dd": round(avg_dd, 2),
                "wf_std_sharpe": round(std_sharpe, 4),
            })

            print(
                f"  {progress} {name}({code}): {status} "
                f"WF_Sharpe={avg_sharpe:.2f} "
                f"Folds={positive_folds}/{len(fold_results)} "
                f"StdSharpe={std_sharpe:.2f}"
            )

        except Exception as e:
            print(f"  {progress} {name}({code}): ERROR ({e})")
            wf_results.append({
                **candidate,
                "wf_status": "error",
                "wf_reason": str(e),
            })

        time.sleep(FETCH_DELAY)

    return wf_results


# ==============================================================
# Phase 4: watchlist.json 自動更新
# ==============================================================

def update_watchlist(wf_results: list, dry_run: bool = False) -> list:
    """WF合格銘柄でwatchlist.jsonを更新する。"""
    passed = [r for r in wf_results if r.get("wf_status") == "pass"]

    print(f"\n{'='*60}")
    print(f"  Phase 4: watchlist 更新")
    print(f"{'='*60}\n")

    if not passed:
        print("  WF合格銘柄なし。watchlistは更新しません。")
        return []

    print(f"  WF合格銘柄: {len(passed)}件\n")
    for r in passed:
        print(
            f"    {r['name']}({r['code']}): "
            f"Sharpe={r['sharpe']:.2f} "
            f"WF_Sharpe={r['wf_avg_sharpe']:.2f} "
            f"Folds={r['wf_positive_folds']}/{r['wf_folds']}"
        )

    if dry_run:
        print("\n  [dry-run] watchlist.jsonの更新をスキップします。")
        return passed

    # 既存のwatchlistを読み込み
    watchlist_path = os.path.join(BASE_DIR, "watchlist.json")
    if os.path.exists(watchlist_path):
        with open(watchlist_path, "r", encoding="utf-8") as f:
            watchlist = json.load(f)
    else:
        watchlist = {"description": "Monthly Momentum監視対象銘柄", "symbols": []}

    # 既存のコードリスト
    existing_codes = {s["code"] for s in watchlist["symbols"]}

    # 新規追加
    added = 0
    for r in passed:
        if r["code"] not in existing_codes:
            watchlist["symbols"].append({
                "code": r["code"],
                "name": r["name"],
                "note": (
                    f"Auto-screened {datetime.now().strftime('%Y-%m-%d')}: "
                    f"Sharpe={r['sharpe']:.2f}, "
                    f"WF_Sharpe={r['wf_avg_sharpe']:.2f}, "
                    f"Folds={r['wf_positive_folds']}/{r['wf_folds']}"
                ),
            })
            added += 1

    watchlist["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(watchlist_path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, indent=2, ensure_ascii=False)

    print(f"\n  watchlist.json 更新: {added}件追加（合計{len(watchlist['symbols'])}件）")
    return passed


# ==============================================================
# 結果保存
# ==============================================================

def save_screening_results(bt_results: list, wf_results: list):
    """screening_results.json に全結果を保存する。"""
    output = {
        "timestamp": datetime.now().isoformat(),
        "config": {
            "min_sharpe": MIN_SHARPE,
            "min_return": MIN_RETURN,
            "min_trades": MIN_TRADES,
            "wf_min_avg_sharpe": WF_MIN_AVG_SHARPE,
            "wf_min_positive_folds": WF_MIN_POSITIVE_FOLDS,
        },
        "backtest_results": bt_results,
        "wf_results": wf_results,
    }

    path = os.path.join(BASE_DIR, "screening_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  結果保存: {path}")


def append_to_experiments(bt_results: list, wf_results: list, passed: list):
    """EXPERIMENTS.md に結果を追記する。"""
    exp_path = os.path.join(BASE_DIR, "EXPERIMENTS.md")

    candidates = filter_candidates(bt_results)
    wf_passed = [r for r in wf_results if r.get("wf_status") == "pass"]

    lines = []
    lines.append(f"\n### 実験: 日本株全自動スクリーニング（{datetime.now().strftime('%Y-%m-%d %H:%M')}）\n\n")
    lines.append(f"- **戦略**: Monthly Momentum（最適化パラメータ使用）\n")
    lines.append(f"- **対象**: 日経225主要{len(bt_results)}銘柄\n")
    lines.append(f"- **担当**: 軍師\n\n")
    lines.append(f"**Phase 2 結果**: {len(candidates)}銘柄が候補（Sharpe>{MIN_SHARPE}, Return>{MIN_RETURN}%）\n\n")

    if candidates:
        lines.append("| 銘柄 | Sharpe | Return | MaxDD | Trades |\n")
        lines.append("|------|--------|--------|-------|--------|\n")
        for r in candidates[:20]:  # 上位20銘柄まで
            lines.append(
                f"| {r['name']}({r['code']}) | {r['sharpe']:.2f} | "
                f"{r['annual_return']:+.1f}% | {r['max_drawdown']:.1f}% | "
                f"{r['total_trades']} |\n"
            )
        lines.append("\n")

    if wf_results:
        lines.append(f"**Phase 3 結果**: {len(wf_passed)}銘柄がWF合格\n\n")
        if wf_passed:
            lines.append("| 銘柄 | BT Sharpe | WF Sharpe | WF正フォールド | 判定 |\n")
            lines.append("|------|-----------|-----------|--------------|------|\n")
            for r in wf_passed:
                lines.append(
                    f"| {r['name']}({r['code']}) | {r['sharpe']:.2f} | "
                    f"{r['wf_avg_sharpe']:.2f} | {r['wf_positive_folds']}/{r['wf_folds']} | PASS |\n"
                )
            lines.append("\n")

    lines.append(f"**結論**: watchlistに{len(wf_passed)}銘柄を追加。signal_monitor.pyで毎朝監視可能。\n\n")

    with open(exp_path, "a", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"  EXPERIMENTS.md に追記: {exp_path}")


# ==============================================================
# メイン
# ==============================================================

def print_summary(bt_results: list, candidates: list, wf_results: list, passed: list):
    """実行結果のサマリーを出力する。"""
    print(f"\n{'='*60}")
    print(f"  SCREENING SUMMARY")
    print(f"{'='*60}")
    print(f"  スキャン銘柄数:     {len(bt_results)}")
    ok_count = sum(1 for r in bt_results if r["status"] == "ok")
    print(f"  バックテスト成功:   {ok_count}")
    print(f"  Phase 2 候補:       {len(candidates)}")
    print(f"  Phase 3 WF検証:     {len(wf_results)}")
    print(f"  Phase 3 合格:       {len(passed)}")

    if passed:
        print(f"\n  合格銘柄:")
        for r in passed:
            print(f"    {r['name']}({r['code']}) Sharpe={r['sharpe']:.2f} WF={r['wf_avg_sharpe']:.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="日本株 Monthly Momentum 全自動スクリーニングパイプライン"
    )
    parser.add_argument(
        "--phase", type=int, choices=[1, 2, 3, 4], default=None,
        help="指定フェーズから開始（デフォルト: 全フェーズ）"
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="WF検証する上位銘柄数（デフォルト: 全候補）"
    )
    parser.add_argument(
        "--period", default="2y",
        help="バックテスト期間（デフォルト: 2y）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="watchlist.jsonを更新しない"
    )
    args = parser.parse_args()

    start_phase = args.phase or 1

    print(f"\n{'#'*60}")
    print(f"  日本株 Monthly Momentum スクリーニングパイプライン")
    print(f"  開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  期間: {args.period}")
    print(f"{'#'*60}")

    # パラメータ読み込み
    params_path = os.path.join(BASE_DIR, "optimized_params.json")
    params = {}
    if os.path.exists(params_path):
        with open(params_path, "r") as f:
            all_params = json.load(f)
        params = all_params.get("monthly", {})
        print(f"\n  最適化パラメータ: {json.dumps(params)}")
    else:
        print("\n  WARNING: optimized_params.json なし。デフォルトパラメータを使用。")

    # --- Phase 1: 銘柄リスト ---
    tickers = load_tickers()
    print(f"  銘柄ユニバース: {len(tickers)}銘柄")

    # --- Phase 2: 一括バックテスト ---
    bt_results = []
    candidates = []
    screening_path = os.path.join(BASE_DIR, "screening_results.json")

    if start_phase <= 2:
        bt_results = run_backtest_screening(tickers, params, period=args.period)
        candidates = filter_candidates(bt_results)
        print(f"\n  Phase 2 完了: {len(candidates)}銘柄が候補")
    elif start_phase == 3:
        # Phase 3から開始する場合、前回の結果を読み込む
        if os.path.exists(screening_path):
            with open(screening_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            bt_results = prev.get("backtest_results", [])
            candidates = filter_candidates(bt_results)
            print(f"\n  前回のスクリーニング結果を読み込み: {len(candidates)}候補")
        else:
            print("  ERROR: screening_results.json が見つかりません。Phase 2から実行してください。")
            sys.exit(1)

    # --- Phase 3: ウォークフォワード検証 ---
    wf_results = []
    if start_phase <= 3 and candidates:
        wf_results = run_walk_forward_validation(
            candidates, params, top_n=args.top, period=args.period
        )

    # --- Phase 4: watchlist更新 ---
    passed = []
    if start_phase <= 4 and wf_results:
        passed = update_watchlist(wf_results, dry_run=args.dry_run)

    # --- 結果保存 ---
    if bt_results:
        save_screening_results(bt_results, wf_results)
    if bt_results or wf_results:
        append_to_experiments(bt_results, wf_results, passed)

    # サマリー
    print_summary(bt_results, candidates, wf_results, passed)

    print(f"  完了: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()

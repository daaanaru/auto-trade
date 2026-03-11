"""
experiment.py -- 戦略パラメータ自動改善ループ（autoresearch方式）

Karpathy の autoresearch の設計思想を auto-trade に応用:
- AIがパラメータを少しずつ変えてバックテストを回す
- 改善したら採用、悪化したらロールバック
- 結果は results.tsv に全記録

使い方:
    python3 experiment.py                    # 1回だけ実験
    python3 experiment.py --cycles 5         # 5回連続実験
    python3 experiment.py --dry-run          # バックテストだけ実行（パラメータ更新しない）

前提:
    - auto-trade/ ディレクトリから実行すること
    - engine.py, strategies/ が同階層にあること
"""

import argparse
import copy
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ============================================================
# パス設定
# experiment.py は autoresearch/ にあるので、親ディレクトリを参照する
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent          # autoresearch/
AUTO_TRADE_DIR = SCRIPT_DIR.parent                     # auto-trade/
PARAMS_FILE = AUTO_TRADE_DIR / "optimized_params.json"  # パラメータファイル
RESULTS_FILE = SCRIPT_DIR / "results.tsv"              # 実験結果ログ

# auto-trade/ を import パスに追加（engine.py や strategies/ を読むため）
sys.path.insert(0, str(AUTO_TRADE_DIR))

# ============================================================
# パラメータの探索範囲（ハードリミット）
# program.md で定義した範囲をコードに落としたもの
# ============================================================
PARAM_BOUNDS = {
    "entry_days": {
        "min": 1,        # 月初1日目から
        "max": 10,       # 月初10日目まで
        "step": 1,       # 整数ステップ
        "type": "int",   # 整数型
    },
    "volume_ma_period": {
        "min": 5,        # 移動平均の最短期間
        "max": 40,       # 移動平均の最長期間
        "step": 1,
        "type": "int",
    },
    "volume_threshold": {
        "min": 0.5,      # 出来高倍率の下限
        "max": 4.0,      # 出来高倍率の上限
        "step": 0.25,    # 0.25刻み
        "type": "float",
    },
}

# WF合格銘柄（verdict=OK のもの）
TARGET_SYMBOLS = [
    "9984.T",   # SoftBank Group
    "8306.T",   # MUFG
    "6758.T",   # Sony（ベスト: WF Sharpe 1.99）
    "6861.T",   # Keyence
    "7974.T",   # Nintendo
]

# ガードレール
MAX_DRAWDOWN_LIMIT = -30.0   # 最大DD がこれを超えたら discard（%）
MAX_PARAMS_PER_EXPERIMENT = 2  # 1回の実験で変えるパラメータの数

# データ取得設定
DATA_PERIOD = "5y"  # 5年分のデータでバックテスト


def load_params():
    """
    optimized_params.json からパラメータを読み込む。
    返すのは monthly セクションのみ（このプロトタイプでは Monthly Momentum だけ扱う）。
    """
    with open(PARAMS_FILE, "r") as f:
        all_params = json.load(f)
    return all_params


def save_params(all_params):
    """
    optimized_params.json にパラメータを書き戻す。
    monthly セクションだけ更新し、他の戦略のパラメータはそのまま残す。
    """
    with open(PARAMS_FILE, "w") as f:
        json.dump(all_params, f, indent=2)
        f.write("\n")


def clamp_value(name, value):
    """
    パラメータを探索範囲内に収める（ハードリミット）。
    はみ出したら境界値に丸める。
    """
    bounds = PARAM_BOUNDS[name]
    if bounds["type"] == "int":
        value = int(round(value))
    clamped = max(bounds["min"], min(bounds["max"], value))
    # ステップに合わせて丸める
    if bounds["type"] == "float":
        step = bounds["step"]
        clamped = round(round(clamped / step) * step, 4)
    return clamped


def generate_mutation(current_monthly_params):
    """
    現在のパラメータから少しだけ変えた「実験版」を生成する。

    変更ルール:
    - 1〜2個のパラメータをランダムに選ぶ
    - 各パラメータを ±1〜3ステップ分だけずらす
    - ハードリミットを超えないように丸める

    Returns:
        (変更後のパラメータ dict, 変更内容の説明 str)
    """
    # 変更するパラメータを1〜2個ランダムに選ぶ
    param_names = list(PARAM_BOUNDS.keys())
    num_changes = random.randint(1, MAX_PARAMS_PER_EXPERIMENT)
    targets = random.sample(param_names, num_changes)

    mutated = copy.deepcopy(current_monthly_params)
    changes = []

    for name in targets:
        bounds = PARAM_BOUNDS[name]
        old_value = mutated.get(name, bounds["min"])
        step = bounds["step"]

        # ±1〜3ステップ分ずらす（方向はランダム）
        delta_steps = random.choice([-3, -2, -1, 1, 2, 3])
        new_value = old_value + delta_steps * step

        # ハードリミット内に収める
        new_value = clamp_value(name, new_value)

        mutated[name] = new_value
        changes.append(f"{name}: {old_value} -> {new_value}")

    change_description = ", ".join(changes)
    return mutated, change_description


def run_backtest(monthly_params):
    """
    WF合格5銘柄に対してバックテストを実行し、平均指標を返す。

    やっていること:
    1. yfinance から各銘柄のデータを取得
    2. Monthly Momentum 戦略を実験パラメータで初期化
    3. engine.py の BacktestEngine でバックテストを実行
    4. 5銘柄の平均シャープ比・リターン・DD・勝率を計算

    Returns:
        dict: {"avg_sharpe", "avg_return", "avg_dd", "avg_winrate"} or None（失敗時）
    """
    # ここで import する（sys.path 設定済み）
    from engine import BacktestEngine, BacktestConfig
    from strategies.monthly_momentum import MonthlyMomentumStrategy

    engine = BacktestEngine(BacktestConfig(initial_capital=1_000_000))
    strategy = MonthlyMomentumStrategy(params=monthly_params)

    results = []
    for symbol in TARGET_SYMBOLS:
        try:
            # yfinance からデータ取得（5年分）
            import yfinance as yf
            data = yf.download(symbol, period=DATA_PERIOD, progress=False)

            if data is None or len(data) < 60:
                print(f"  [SKIP] {symbol}: データ不足（{len(data) if data is not None else 0}行）")
                continue

            # カラム名を小文字に統一（yfinance の仕様に合わせる）
            data.columns = [c.lower() if isinstance(c, str) else c for c in data.columns]
            # MultiIndex の場合（yfinance v0.2.31+）はフラット化
            if hasattr(data.columns, 'levels'):
                data.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in data.columns]

            result = engine.run(strategy, data, verbose=False)
            results.append({
                "symbol": symbol,
                "sharpe": result.sharpe_ratio,
                "annual_return": result.annual_return,
                "max_dd": result.max_drawdown,
                "win_rate": result.win_rate,
            })

        except Exception as e:
            print(f"  [ERROR] {symbol}: {e}")
            continue

    if not results:
        return None

    # 平均指標を計算
    avg = {
        "avg_sharpe": sum(r["sharpe"] for r in results) / len(results),
        "avg_return": sum(r["annual_return"] for r in results) / len(results),
        "avg_dd": sum(r["max_dd"] for r in results) / len(results),
        "avg_winrate": sum(r["win_rate"] for r in results) / len(results),
        "details": results,
    }
    return avg


def record_result(experiment_id, params, metrics, baseline_sharpe, improved, accepted, change_desc):
    """
    実験結果を results.tsv に追記する。
    TSV形式で1行ずつ記録していく（Excelやスプレッドシートで開ける）。
    """
    row = "\t".join([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # timestamp
        str(experiment_id),                              # experiment_id
        change_desc,                                     # changed_params
        str(params.get("entry_days", "")),               # entry_days
        str(params.get("volume_ma_period", "")),         # volume_ma_period
        str(params.get("volume_threshold", "")),         # volume_threshold
        f"{metrics['avg_sharpe']:.4f}",                  # avg_sharpe
        f"{metrics['avg_return']:.2f}",                  # avg_return
        f"{metrics['avg_dd']:.2f}",                      # avg_dd
        f"{metrics['avg_winrate']:.1f}",                 # avg_winrate
        f"{baseline_sharpe:.4f}",                        # baseline_sharpe
        str(improved),                                   # improved
        str(accepted),                                   # accepted
    ])

    with open(RESULTS_FILE, "a") as f:
        f.write(row + "\n")


def git_commit_improvement(experiment_id, change_desc, new_sharpe, old_sharpe):
    """
    パラメータ改善時に git commit する。
    悪化時は何もしない（ロールバック = 元のファイルのまま）。
    """
    try:
        subprocess.run(
            ["git", "add", str(PARAMS_FILE)],
            cwd=str(AUTO_TRADE_DIR),
            check=True,
            capture_output=True,
        )
        msg = (
            f"autoresearch: exp#{experiment_id} Sharpe {old_sharpe:.3f} -> {new_sharpe:.3f}\n"
            f"\n"
            f"Changed: {change_desc}\n"
            f"Method: autoresearch experiment loop"
        )
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(AUTO_TRADE_DIR),
            check=True,
            capture_output=True,
        )
        print(f"  [GIT] committed: exp#{experiment_id}")
    except subprocess.CalledProcessError as e:
        print(f"  [GIT] commit failed: {e.stderr.decode() if e.stderr else e}")


def run_one_experiment(experiment_id, dry_run=False):
    """
    1回の実験サイクルを実行する。

    流れ:
    1. 現在のパラメータを読む（ベースライン）
    2. ベースラインのバックテストを実行
    3. パラメータを少し変更した実験版を作る
    4. 実験版でバックテストを実行
    5. 結果を比較して採用 or ロールバック
    """
    print(f"\n{'='*60}")
    print(f"  Experiment #{experiment_id}")
    print(f"{'='*60}")

    # --- 1. 現在のパラメータを読む ---
    all_params = load_params()
    current_monthly = copy.deepcopy(all_params["monthly"])
    print(f"  Current params: {current_monthly}")

    # --- 2. ベースラインのバックテスト ---
    print(f"\n  [BASELINE] Running backtest...")
    baseline = run_backtest(current_monthly)
    if baseline is None:
        print("  [ABORT] Baseline backtest failed")
        return False
    baseline_sharpe = baseline["avg_sharpe"]
    print(f"  [BASELINE] Avg Sharpe: {baseline_sharpe:.4f}, Avg DD: {baseline['avg_dd']:.2f}%")

    # --- 3. パラメータを変異させる ---
    mutated_params, change_desc = generate_mutation(current_monthly)
    print(f"\n  [MUTATION] {change_desc}")
    print(f"  [MUTATION] New params: {mutated_params}")

    # --- 4. 実験版のバックテスト ---
    print(f"\n  [EXPERIMENT] Running backtest...")
    experiment_result = run_backtest(mutated_params)
    if experiment_result is None:
        print("  [ABORT] Experiment backtest failed")
        record_result(experiment_id, mutated_params,
                      {"avg_sharpe": 0, "avg_return": 0, "avg_dd": 0, "avg_winrate": 0},
                      baseline_sharpe, False, False, change_desc)
        return False
    exp_sharpe = experiment_result["avg_sharpe"]
    exp_dd = experiment_result["avg_dd"]
    print(f"  [EXPERIMENT] Avg Sharpe: {exp_sharpe:.4f}, Avg DD: {exp_dd:.2f}%")

    # --- 5. 判定 ---
    # ガードレール: 最大DDチェック（個別銘柄で-30%超がないか）
    worst_dd = min(r["max_dd"] for r in experiment_result["details"])
    dd_violation = worst_dd < MAX_DRAWDOWN_LIMIT

    if dd_violation:
        print(f"  [DISCARD] DD violation: worst DD = {worst_dd:.2f}% (limit: {MAX_DRAWDOWN_LIMIT}%)")
        improved = False
        accepted = False
    elif exp_sharpe > baseline_sharpe:
        print(f"  [IMPROVED] Sharpe: {baseline_sharpe:.4f} -> {exp_sharpe:.4f} (+{exp_sharpe - baseline_sharpe:.4f})")
        improved = True
        accepted = not dry_run
    else:
        print(f"  [NO IMPROVEMENT] Sharpe: {baseline_sharpe:.4f} -> {exp_sharpe:.4f}")
        improved = False
        accepted = False

    # --- 6. 結果記録 ---
    record_result(experiment_id, mutated_params, experiment_result,
                  baseline_sharpe, improved, accepted, change_desc)

    # --- 7. 採用 or ロールバック ---
    if accepted:
        # パラメータを更新
        all_params["monthly"] = mutated_params
        save_params(all_params)
        print(f"  [ACCEPT] Parameters updated in optimized_params.json")
        # git commit
        git_commit_improvement(experiment_id, change_desc, exp_sharpe, baseline_sharpe)
    else:
        # 何もしない = ロールバック（元のパラメータのまま）
        print(f"  [ROLLBACK] Parameters unchanged")

    return improved


def main():
    """
    メイン関数。コマンドライン引数を解析して実験を実行する。
    """
    parser = argparse.ArgumentParser(
        description="auto-trade 戦略パラメータ自動改善ループ（autoresearch方式）"
    )
    parser.add_argument(
        "--cycles", type=int, default=1,
        help="実験を何回繰り返すか（デフォルト: 1）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="バックテストだけ実行し、パラメータは更新しない"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  autoresearch: Strategy Parameter Improvement Loop")
    print("  Target: Monthly Momentum x JP Stocks (WF-passed)")
    print(f"  Cycles: {args.cycles}  Dry-run: {args.dry_run}")
    print("=" * 60)

    # 既存の実験番号を取得（results.tsv の行数から）
    start_id = 1
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, "r") as f:
            lines = f.readlines()
            # ヘッダー行を除いた実データ行数
            data_lines = [l for l in lines[1:] if l.strip()]
            start_id = len(data_lines) + 1

    improvements = 0
    for i in range(args.cycles):
        exp_id = start_id + i
        try:
            result = run_one_experiment(exp_id, dry_run=args.dry_run)
            if result:
                improvements += 1
        except KeyboardInterrupt:
            print("\n  [INTERRUPTED] User stopped the experiment")
            break
        except Exception as e:
            print(f"\n  [ERROR] Experiment #{exp_id} failed: {e}")
            import traceback
            traceback.print_exc()

        # 連続実行時は少し待つ（yfinance のレート制限対策）
        if i < args.cycles - 1:
            print("  [WAIT] 5 seconds before next cycle...")
            time.sleep(5)

    # サマリー
    print(f"\n{'='*60}")
    print(f"  Summary: {improvements}/{args.cycles} experiments improved")
    print(f"  Results logged in: {RESULTS_FILE}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

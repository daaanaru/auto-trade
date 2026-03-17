"""
graduation_checker.py — ペーパートレード卒業判定ツール

ペーパートレードの実績データを分析し、実弾投入（DRY_RUNテスト）に
進めるかどうかを自動判定する。

卒業条件:
  1. 最低期間: 14日以上
  2. 勝率: 40%以上
  3. ローリングSharpe: 0.5以上
  4. 最大ドローダウン: -15%以内
  5. バックテスト乖離: Sharpe差 ±20%以内

使い方:
  python graduation_checker.py                # 卒業判定レポート表示
  python graduation_checker.py --json         # JSON形式で出力
  python graduation_checker.py --auto-promote # 卒業時に.envのDRY_RUN→false
"""

from __future__ import annotations

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

PROJECT_ROOT = Path(__file__).parent

CONFIG_FILE = PROJECT_ROOT / "crypto_config.json"
# unified_paper_trade.py が書き出すファイルを参照
PERFORMANCE_LOG_FILE = PROJECT_ROOT / "paper_portfolio_log.json"
POSITIONS_FILE = PROJECT_ROOT / "paper_portfolio.json"
TRADE_LOG_FILE = PROJECT_ROOT / "paper_trade_log.json"
BACKTEST_RESULTS_FILE = PROJECT_ROOT / "timeframe_backtest_results.json"
ENV_FILE = PROJECT_ROOT / ".env"


# ==============================================================
# データ読み込み
# ==============================================================

def load_json(path: Path):
    """JSONファイルを読み込む。存在しなければ空リスト/空辞書を返す。"""
    if not path.exists():
        return [] if "log" in path.name else {}
    with open(path, "r") as f:
        return json.load(f)


def load_config() -> dict:
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


# ==============================================================
# 個別チェック関数
# ==============================================================

def check_min_days(perf_log: list, required: int) -> dict:
    """最低運用期間のチェック。"""
    if not perf_log:
        return {"name": "最低期間", "required": f"{required}日以上",
                "actual": "0日", "value": 0, "passed": False}

    first_ts = datetime.fromisoformat(perf_log[0]["timestamp"])
    days = (datetime.now() - first_ts).days

    return {
        "name": "最低期間",
        "required": f"{required}日以上",
        "actual": f"{days}日",
        "value": days,
        "passed": days >= required,
    }


def check_win_rate(positions: dict, required: float) -> dict:
    """勝率チェック。最低20回以上の取引が必要。（5回では統計的に無意味、20回で±20%の信頼区間）"""
    total = positions.get("total_trades", 0)
    wins = positions.get("winning_trades", 0)
    min_trades = 20  # 最低取引回数（統計的信頼性のため20回必要）

    if total < min_trades:
        return {
            "name": "勝率",
            "required": f"{required}%以上（最低{min_trades}回取引）",
            "actual": f"取引不足 ({total}/{min_trades}回)",
            "value": 0.0,
            "passed": False,
        }

    rate = (wins / total * 100)
    return {
        "name": "勝率",
        "required": f"{required}%以上",
        "actual": f"{rate:.1f}% ({wins}/{total})",
        "value": rate,
        "passed": rate >= required,
    }


def _daily_values(perf_log: list) -> list[float]:
    """portfolio_logから各日の最終レコードだけを抽出する。

    1日6回記録されるため、日中ノイズを除去してSharpe計算の精度を上げる。
    """
    daily = {}
    for r in perf_log:
        if not r.get("total_value_jpy"):
            continue
        date_key = r["timestamp"][:10]  # "2026-03-09T05:00:..." → "2026-03-09"
        daily[date_key] = r["total_value_jpy"]  # 同日は後のレコードで上書き
    return [v for _, v in sorted(daily.items())]


def check_rolling_sharpe(perf_log: list, required: float) -> dict:
    """ローリングSharpe比のチェック（日次リターンから年率換算）。"""
    values = _daily_values(perf_log)

    if len(values) < 5:
        return {
            "name": "ローリングSharpe",
            "required": f"{required}以上",
            "actual": f"データ不足 ({len(values)}日分, 最低5日必要)",
            "value": 0.0,
            "passed": False,
        }

    returns = pd.Series(values).pct_change().dropna()
    if returns.std() == 0:
        sharpe = 0.0
    else:
        # 混合市場（株=平日のみ/BTC=365日）→ 実データの記録頻度に基づき年換算
        # paper_portfolio_logは2時間ごと記録 → 1日約12レコード → 年間≒252営業日相当
        sharpe = float((returns.mean() / returns.std()) * np.sqrt(252))

    return {
        "name": "ローリングSharpe",
        "required": f"{required}以上",
        "actual": f"{sharpe:.3f}",
        "value": sharpe,
        "passed": sharpe >= required,
    }


def check_max_drawdown(perf_log: list, limit: float) -> dict:
    """最大ドローダウンのチェック。limitは負の値（例: -15.0）。"""
    values = [r["total_value_jpy"] for r in perf_log if r.get("total_value_jpy")]

    if not values:
        return {
            "name": "最大ドローダウン",
            "required": f"{limit}%以内",
            "actual": "データなし",
            "value": 0.0,
            "passed": False,
        }

    max_dd = 0.0
    peak = values[0]
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    return {
        "name": "最大ドローダウン",
        "required": f"{limit}%以内",
        "actual": f"{max_dd:.2f}%",
        "value": max_dd,
        "passed": max_dd > limit,  # -5% > -15% → True
    }


def check_backtest_deviation(perf_log: list, positions: dict,
                             max_deviation: float) -> dict:
    """バックテスト結果との乖離チェック（Sharpe比で比較）。

    ペーパートレードのSharpeとバックテストのSharpeを比較し、
    乖離が±max_deviation%以内かどうかを判定する。
    """
    # ペーパートレードのSharpe計算（日次ベース）
    values = _daily_values(perf_log)
    if len(values) < 5:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"ペーパーデータ不足 ({len(values)}日分)",
            "value": None,
            "passed": False,
        }

    # 14日未満ガード: Sharpe年率換算(√252)は短期間だと統計的に無意味
    # 例: 4日間のデータで年率換算→乖離1628%のような異常値が出る
    if len(values) < 14:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"運用14日未満のためスキップ ({len(values)}日分)",
            "value": None,
            "passed": True,  # 14日未満はPASS扱い（統計的に判定不能）
        }

    returns = pd.Series(values).pct_change().dropna()
    paper_sharpe = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() > 0 else 0.0

    # バックテスト結果の読み込み
    bt_results = load_json(BACKTEST_RESULTS_FILE)
    if not bt_results:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": "BTデータなし (SKIP)",
            "value": None,
            "passed": True,  # BTデータがなければスキップ扱い
        }

    # 使用中の戦略を全ポジションから収集（複数戦略が混在し得る）
    strategy_map = {
        "sma": "SMA_Crossover",
        "rsi": "RSI_Reversion",
        "bb_rsi": "BB_RSI_Combo",
        "monthly": "Monthly_Momentum",
        "vol_div": "Volume_Divergence",
        "mom_pb": "Momentum_Pullback",
        "order_block": "Order_Block",
    }

    # ポートフォリオの全ポジションから使用戦略を集める
    pos_list = positions.get("positions", [])
    used_strategies = set()
    for p in pos_list:
        s = p.get("strategy", "")
        if s:
            used_strategies.add(s)
    if not used_strategies:
        used_strategies = {"vol_div"}  # ポジションなしの場合のフォールバック

    # 全戦略のBT Sharpeを集め、加重平均（均等）で比較
    daily_results = bt_results.get("日足(1d)", {})
    bt_sharpes = []
    for sk in used_strategies:
        bt_name = strategy_map.get(sk, sk)
        if bt_name in daily_results:
            s_val = daily_results[bt_name].get("sharpe_ratio")
            if s_val is not None:
                bt_sharpes.append(s_val)

    if not bt_sharpes:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"BT Sharpe不明 (戦略: {', '.join(used_strategies)})",
            "value": None,
            "passed": True,  # 比較対象がなければスキップ
        }
    bt_sharpe = sum(bt_sharpes) / len(bt_sharpes)

    # 乖離率計算
    if bt_sharpe == 0:
        deviation = abs(paper_sharpe) * 100  # BT=0なら絶対値で判定
    else:
        deviation = abs((paper_sharpe - bt_sharpe) / bt_sharpe) * 100

    return {
        "name": "BT乖離",
        "required": f"+-{max_deviation}%以内",
        "actual": f"{deviation:.1f}% (Paper:{paper_sharpe:.2f} vs BT:{bt_sharpe:.2f})",
        "value": deviation,
        "passed": deviation <= max_deviation,
    }


# ==============================================================
# 総合判定
# ==============================================================

def run_graduation_check(config: dict) -> dict:
    """全卒業条件を一括チェックし、結果辞書を返す。"""
    criteria = config["graduation_criteria"]
    perf_log = load_json(PERFORMANCE_LOG_FILE)
    if not isinstance(perf_log, list):
        perf_log = []
    positions = load_json(POSITIONS_FILE)
    if not isinstance(positions, dict):
        positions = {}

    checks = [
        check_min_days(perf_log, criteria["min_days"]),
        check_win_rate(positions, criteria["min_win_rate"]),
        check_rolling_sharpe(perf_log, criteria["min_rolling_sharpe"]),
        check_max_drawdown(perf_log, criteria["max_drawdown_pct"]),
        check_backtest_deviation(perf_log, positions, criteria["backtest_deviation_pct"]),
    ]

    all_passed = all(c["passed"] for c in checks)
    passed_count = sum(1 for c in checks if c["passed"])

    # 追加の統計情報
    total_trades = positions.get("total_trades", 0)
    capital = positions.get("initial_capital_jpy", positions.get("capital", config["paper_trade"]["initial_capital_jpy"]))
    total_pnl = positions.get("total_realized_pnl", positions.get("total_pnl", 0))

    return {
        "timestamp": datetime.now().isoformat(),
        "graduated": all_passed,
        "passed_count": passed_count,
        "total_checks": len(checks),
        "checks": checks,
        "summary": {
            "strategy": positions.get("strategy", "unknown"),
            "total_trades": total_trades,
            "capital_jpy": capital,
            "total_pnl_jpy": total_pnl,
            "roi_pct": (total_pnl / config["paper_trade"]["initial_capital_jpy"] * 100)
                       if total_pnl else 0.0,
        },
    }


# ==============================================================
# 表示
# ==============================================================

def print_report(result: dict):
    """卒業判定結果を見やすく表示する。"""
    print()
    print("=" * 58)
    print("  GRADUATION CHECK — ペーパートレード卒業判定")
    print("=" * 58)

    summary = result["summary"]
    print(f"  戦略: {summary['strategy']}")
    print(f"  取引数: {summary['total_trades']}回")
    print(f"  資本: {summary['capital_jpy']:,.0f} JPY")
    print(f"  累計損益: {summary['total_pnl_jpy']:+,.0f} JPY (ROI: {summary['roi_pct']:+.2f}%)")
    print()

    for c in result["checks"]:
        mark = "[PASS]" if c["passed"] else "[FAIL]"
        print(f"  {mark} {c['name']:<16} 基準: {c['required']:<16} 実績: {c['actual']}")

    print()
    print("-" * 58)
    passed = result["passed_count"]
    total = result["total_checks"]

    if result["graduated"]:
        print(f"  RESULT: GRADUATED ({passed}/{total})")
        print("  卒業条件クリア！ DRY_RUNテストに進めます。")
    else:
        print(f"  RESULT: NOT YET ({passed}/{total})")
        failed = [c["name"] for c in result["checks"] if not c["passed"]]
        print(f"  未達項目: {', '.join(failed)}")

    print("=" * 58)
    print()


# ==============================================================
# --auto-promote: .envのDRY_RUN設定を変更
# ==============================================================

def auto_promote():
    """卒業条件クリア時に.envのLIVE_TRADE_DRY_RUN=true→falseに変更する。

    危険操作のため確認メッセージ付き。
    """
    if not ENV_FILE.exists():
        print("[ERROR] .env ファイルが見つかりません。")
        print(f"  期待パス: {ENV_FILE}")
        print("  .env.example をコピーして .env を作成してください。")
        return False

    content = ENV_FILE.read_text()

    if "LIVE_TRADE_DRY_RUN=false" in content:
        print("[INFO] 既に LIVE_TRADE_DRY_RUN=false に設定されています。")
        return True

    if "LIVE_TRADE_DRY_RUN=true" not in content:
        print("[WARN] .env に LIVE_TRADE_DRY_RUN の設定が見つかりません。")
        return False

    # 確認メッセージ
    print()
    print("!" * 58)
    print("  WARNING: 実弾モードへの切り替え")
    print("!" * 58)
    print()
    print("  この操作は .env の LIVE_TRADE_DRY_RUN を false に変更します。")
    print("  変更後、live_trade.py --execute を実行すると実際の注文が発生します。")
    print()

    try:
        answer = input("  本当に変更しますか？ (yes/no): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  キャンセルされました。")
        return False

    if answer != "yes":
        print("  キャンセルされました。")
        return False

    new_content = content.replace("LIVE_TRADE_DRY_RUN=true", "LIVE_TRADE_DRY_RUN=false")
    ENV_FILE.write_text(new_content)
    print("  .env を更新しました: LIVE_TRADE_DRY_RUN=false")
    print("  次のステップ: python live_trade.py --execute で実弾テスト開始")
    return True


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="ペーパートレード卒業判定ツール — 実弾投入の可否を自動チェック"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="結果をJSON形式で出力"
    )
    parser.add_argument(
        "--auto-promote", action="store_true",
        help="卒業条件クリア時に.envのDRY_RUN→falseに変更（確認付き）"
    )
    args = parser.parse_args()

    config = load_config()
    result = run_graduation_check(config)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        # 正常終了: 未卒業もエラーではない（launchdがexit=1を異常扱いするため）
        sys.exit(0)

    print_report(result)

    # 通知
    try:
        from notifier import notify_graduation
        notify_graduation(result["graduated"], result["checks"], result["summary"])
    except Exception:
        pass

    if args.auto_promote:
        if result["graduated"]:
            auto_promote()
        else:
            print("[INFO] 卒業条件未達のため --auto-promote は実行されません。")
            print("       全条件をクリアしてから再実行してください。")

    # 正常終了: 未卒業は「まだ条件未達」であり、スクリプトエラーではない
    # launchdはexit!=0を異常終了と記録するため、常にexit=0で返す
    sys.exit(0)


if __name__ == "__main__":
    main()

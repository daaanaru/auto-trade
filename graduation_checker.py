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
    # closed_tradesから正確に集計（カウンタフィールドはFORCE_CLOSE_ALL等で更新漏れあり）
    closed_trades = positions.get("closed_trades", [])
    total = len(closed_trades)
    wins = sum(1 for t in closed_trades if t.get("net_pnl_jpy", 0) > 0)
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

    注意（バグ防止）:
    - √252による年率換算は短期（14日未満）では統計的に無意味
    - 例: 4日間で +0.5%ずつ → Sharpe=∞（std≈0） → 乖離5113%のような異常値
    - 対策: 14日未満は判定スキップ + 日数ログで将来検出を容易に
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
    # 根拠: 4日間のデータで年率換算→乖離5113%のような異常値が出ていた（過去事例）
    # 統計的に有意な結果には最低14日間のデータが必要（240リターンサンプル程度）
    if len(values) < 14:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"運用14日未満のためスキップ ({len(values)}日分)",
            "value": None,
            "passed": True,  # 14日未満はPASS扱い（統計的に判定不能）
        }

    returns = pd.Series(values).pct_change().dropna()
    # std=0 の場合（日次リターンがほぼ一定）を Sharpe=0 で安全に処理
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
        "volscale_sma": "VolScale_SMA",
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
    # |bt_sharpe| < 0.3 のとき比率ベースだと分母爆発（数千%の異常値）になるので
    # Sharpe差の絶対値にフォールバックする
    sharpe_diff = abs(paper_sharpe - bt_sharpe)

    # 異常値検出: Paper Sharpe が |50| を超える場合は短期間の √252 異常の可能性
    # （統計的に Sharpe > 10 は年単位でも稀。< 14日で出ることはあり得ない）
    if abs(paper_sharpe) > 50:
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"異常値検出: Sharpe={paper_sharpe:.1f} (短期sqrt(252)バグの可能性)",
            "value": None,
            "passed": False,
        }

    if abs(bt_sharpe) < 0.3:
        # BT Sharpe が0付近 → 比率計算が無意味。Sharpe差で判定
        # 差が1.0以上なら有意な乖離とみなす
        deviation_display = f"Sharpe差 {sharpe_diff:.2f} (Paper:{paper_sharpe:.2f} vs BT:{bt_sharpe:.2f})"
        passed = sharpe_diff < 1.0
        return {
            "name": "BT乖離",
            "required": f"Sharpe差1.0未満 (BT≈0のため絶対値判定)",
            "actual": deviation_display,
            "value": sharpe_diff,
            "passed": passed,
        }
    else:
        deviation = sharpe_diff / abs(bt_sharpe) * 100
        return {
            "name": "BT乖離",
            "required": f"+-{max_deviation}%以内",
            "actual": f"{deviation:.1f}% (Paper:{paper_sharpe:.2f} vs BT:{bt_sharpe:.2f})",
            "value": deviation,
            "passed": deviation <= max_deviation,
        }


# ==============================================================
# 戦略別判定ロジック
# ==============================================================

def collect_strategy_trades(positions: dict) -> dict[str, list]:
    """closed_trades を strategy ごとに集約する。

    Returns:
        { "vol_div": [...], "bb_rsi": [...], ... }
    """
    trades_by_strategy = {}
    for trade in positions.get("closed_trades", []):
        strategy = trade.get("strategy", "unknown")
        if strategy not in trades_by_strategy:
            trades_by_strategy[strategy] = []
        trades_by_strategy[strategy].append(trade)
    return trades_by_strategy


def _strategy_daily_values(trades: list, perf_log: list) -> list[float]:
    """特定戦略の決済履歴から日別ポートフォリオ値を復元する。

    注: 厳密には「その戦略のみの当日P&L」を計算することに相当。
    """
    if not trades:
        return []

    # 戦略の最初の取引日時から最後の決済日時までのカレンダーを構築
    trade_dates = set()
    for t in trades:
        exit_date = t.get("exit_date", "")
        if exit_date:
            date_key = exit_date[:10]
            trade_dates.add(date_key)

    # perf_log からその日のデータを抽出（ない場合は前日値を踏襲）
    daily_dict = {}
    last_value = 0
    for date_key in sorted(trade_dates):
        # その日のレコードを perf_log から探す
        found = False
        for r in perf_log:
            if r.get("timestamp", "")[:10] == date_key:
                # 戦略別の日次P&Lを計算: 「その戦略の累計決済P&L」
                strategy_pnl = sum(tr.get("net_pnl_jpy", 0) for tr in trades if tr.get("exit_date", "")[:10] <= date_key)
                daily_dict[date_key] = strategy_pnl
                last_value = strategy_pnl
                found = True

        if not found and date_key in [k for k in daily_dict.keys()]:
            daily_dict[date_key] = last_value

    return [v for _, v in sorted(daily_dict.items())]


def check_strategy_win_rate(trades: list, required: float) -> dict:
    """戦略別の勝率チェック。

    全体判定（20回基準）よりも甘い3回基準を適用。
    理由: 戦略別フィルタリング時点では複数戦略の混在により
    単一戦略のサンプルが限定される。統計的に信頼できる下限は3回。
    """
    total = len(trades)
    wins = sum(1 for t in trades if t.get("net_pnl_jpy", 0) > 0)
    min_trades = 3  # 戦略別は3回以上で評価（全体の20回より緩和）

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


def check_strategy_rolling_sharpe(trades: list, required: float) -> dict:
    """戦略別ローリングSharpe計算。決済日ベースの累積P&Lから日次変化を算出。

    少サンプル対応: 決済が3件以上あれば日次変化でSharpe算出。
    短期データ警告: √252による年率換算は10件未満では統計的に無意味。
    (例: 4件全勝利だけで Sharpe 29 のような異常値が出る)
    """
    if len(trades) < 3:
        return {
            "name": "ローリングSharpe",
            "required": f"{required}以上",
            "actual": f"データ不足 ({len(trades)}件取引, 最低3件必要)",
            "value": 0.0,
            "passed": False,
        }

    # 決済日ごとにソートし、各決済のP&L（金額）を取得
    sorted_trades = sorted(trades, key=lambda t: t.get("exit_date", ""))
    pnl_values = [t.get("net_pnl_jpy", 0) for t in sorted_trades]

    # 初期資本を30万円と仮定して相対リターンに変換
    initial_capital = 300000
    cumulative_pnl = 0
    cumulative_series = [initial_capital]
    for pnl in pnl_values:
        cumulative_pnl += pnl
        cumulative_series.append(initial_capital + cumulative_pnl)

    # 日次リターン率を計算
    returns = pd.Series(cumulative_series).pct_change().dropna()

    if len(returns) < 2 or returns.std() == 0:
        sharpe = 0.0
    else:
        sharpe = float((returns.mean() / returns.std()) * np.sqrt(252))

    # 短期データへの異常値警告（10件未満）
    # 理由: √252による年率換算は、高い変動率で過度に大きな値になる
    # 例: 4件全勝利のみで Sharpe 29 のような異常値
    if len(trades) < 10 and abs(sharpe) > 5:
        return {
            "name": "ローリングSharpe",
            "required": f"{required}以上",
            "actual": f"{sharpe:.3f} ⚠️ （少サンプル注意: {len(trades)}件取引、年率換算が過度に大きい）",
            "value": sharpe,
            "passed": False,  # 少サンプルの異常値は条件不達として扱う
        }

    return {
        "name": "ローリングSharpe",
        "required": f"{required}以上",
        "actual": f"{sharpe:.3f}",
        "value": sharpe,
        "passed": sharpe >= required,
    }


def check_strategy_max_drawdown(trades: list, limit: float) -> dict:
    """戦略別の最大ドローダウン。"""
    if not trades:
        return {
            "name": "最大ドローダウン",
            "required": f"{limit}%以内",
            "actual": "データなし",
            "value": 0.0,
            "passed": False,
        }

    sorted_trades = sorted(trades, key=lambda t: t.get("exit_date", ""))
    initial_capital = 300000
    cumulative_pnl = [0]
    for t in sorted_trades:
        cumulative_pnl.append(cumulative_pnl[-1] + t.get("net_pnl_jpy", 0))

    values = [initial_capital + pnl for pnl in cumulative_pnl]
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
        "passed": max_dd > limit,
    }


def run_strategy_graduation_check(config: dict, positions: dict) -> dict:
    """戦略別の卒業判定を実行。"""
    criteria = config["graduation_criteria"]
    trades_by_strategy = collect_strategy_trades(positions)

    strategy_results = {}
    for strategy_name, trades in trades_by_strategy.items():
        checks = [
            check_strategy_win_rate(trades, criteria["min_win_rate"]),
            check_strategy_rolling_sharpe(trades, criteria["min_rolling_sharpe"]),
            check_strategy_max_drawdown(trades, criteria["max_drawdown_pct"]),
        ]

        all_passed = all(c["passed"] for c in checks)
        total_pnl = sum(t.get("net_pnl_jpy", 0) for t in trades)

        strategy_results[strategy_name] = {
            "graduated": all_passed,
            "checks": checks,
            "summary": {
                "total_trades": len(trades),
                "total_pnl_jpy": total_pnl,
                "roi_pct": (total_pnl / 300000 * 100),
            }
        }

    # 卒業可能な戦略をフィルタ
    graduated_strategies = [
        s for s, r in strategy_results.items() if r["graduated"]
    ]

    return {
        "strategy_breakdown": strategy_results,
        "graduated_strategies": graduated_strategies,
        "recommendation": (
            f"条件クリア: {', '.join(graduated_strategies)}"
            if graduated_strategies
            else "単一戦略での卒業条件クリアなし（複合戦略の詳細分析要）"
        ),
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

    # 追加の統計情報（closed_tradesから正確に集計）
    closed_trades_all = positions.get("closed_trades", [])
    total_trades = len(closed_trades_all)
    # 初期資本: ポートフォリオ実績値 → config のフォールバック
    # crypto_config.json の paper_trade.initial_capital_jpy は旧BTC単体用(30,000)なので
    # 統合ペーパートレード(300,000)の実績と齟齬が出る。ポートフォリオ側を優先する
    capital = positions.get("initial_capital_jpy", config["paper_trade"]["initial_capital_jpy"])
    total_pnl = sum(t.get("net_pnl_jpy", 0) for t in closed_trades_all)

    # 戦略別卒業判定（新規追加）
    strategy_breakdown = run_strategy_graduation_check(config, positions)

    return {
        "timestamp": datetime.now().isoformat(),
        "graduated": all_passed,
        "passed_count": passed_count,
        "total_checks": len(checks),
        "checks": checks,
        "summary": {
            "strategy": ", ".join(sorted({p.get("strategy", "?") for p in positions.get("positions", [])})) or positions.get("strategy", "mixed"),
            "total_trades": total_trades,
            "capital_jpy": capital,
            "total_pnl_jpy": total_pnl,
            "roi_pct": (total_pnl / capital * 100) if (total_pnl is not None and capital) else 0.0,
        },
        "strategy_breakdown": strategy_breakdown["strategy_breakdown"],
        "graduated_strategies": strategy_breakdown["graduated_strategies"],
        "recommendation": strategy_breakdown["recommendation"],
    }


# ==============================================================
# 表示
# ==============================================================

def print_report(result: dict):
    """卒業判定結果を見やすく表示する。"""
    print()
    print("=" * 70)
    print("  GRADUATION CHECK — ペーパートレード卒業判定")
    print("=" * 70)

    summary = result["summary"]
    print(f"  戦略: {summary['strategy']}")
    print(f"  取引数: {summary['total_trades']}回")
    print(f"  資本: {summary['capital_jpy']:,.0f} JPY")
    print(f"  累計損益: {summary['total_pnl_jpy']:+,.0f} JPY (ROI: {summary['roi_pct']:+.2f}%)")
    print()

    for c in result["checks"]:
        mark = "[PASS]" if c["passed"] else "[FAIL]"
        print(f"  {mark} {c['name']:<16} 基準: {c['required']:<20} 実績: {c['actual']}")

    print()
    print("-" * 70)
    passed = result["passed_count"]
    total = result["total_checks"]

    if result["graduated"]:
        print(f"  RESULT: GRADUATED ({passed}/{total})")
        print("  卒業条件クリア！ DRY_RUNテストに進めます。")
    else:
        print(f"  RESULT: NOT YET ({passed}/{total})")
        failed = [c["name"] for c in result["checks"] if not c["passed"]]
        print(f"  未達項目: {', '.join(failed)}")

    # 戦略別判定（テーブル形式に改善）
    print()
    print("=" * 70)
    print("  戦略別卒業判定（単独で条件クリアした戦略）")
    print("=" * 70)

    strategy_breakdown = result.get("strategy_breakdown", {})
    if strategy_breakdown:
        # ヘッダー行
        print()
        print(f"  {'戦略名':<18} {'状態':^10} {'取引':>4} {'損益':>12} {'ROI':>8} {'勝率':>6}")
        print("  " + "-" * 66)

        for strategy_name, strategy_result in sorted(strategy_breakdown.items()):
            status_mark = "✅" if strategy_result["graduated"] else "❌"
            status_text = "PASSED" if strategy_result["graduated"] else "FAILED"
            summary = strategy_result["summary"]

            # 勝率を計算
            checks = strategy_result.get("checks", [])
            win_rate_check = next((c for c in checks if c["name"] == "勝率"), None)
            win_rate_str = win_rate_check["actual"].split("(")[0].strip() if win_rate_check else "N/A"

            print(f"  {strategy_name:<18} {status_mark} {status_text:<7} {summary['total_trades']:>4}回 " +
                  f"{summary['total_pnl_jpy']:>+12.0f} {summary['roi_pct']:>+7.2f}% {win_rate_str:>6}")

        print()
        print("  詳細判定:")
        print("  " + "-" * 66)
        for strategy_name, strategy_result in sorted(strategy_breakdown.items()):
            print()
            print(f"  ▼ {strategy_name}")
            for c in strategy_result.get("checks", []):
                mark = "✓" if c["passed"] else "✗"
                print(f"    {mark} {c['name']:<16} {c['required']:<20} → {c['actual']}")
    else:
        print("  戦略別データなし")

    print()
    print("-" * 70)
    recommendation = result.get("recommendation", "")
    graduated_strategies = result.get("graduated_strategies", [])

    if graduated_strategies:
        print(f"  💡 推奨: {recommendation}")
        print(f"  実弾移行可能な戦略:")
        for strat in graduated_strategies:
            print(f"    → {strat}")
    else:
        print(f"  💡 {recommendation}")

    print("=" * 70)
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
        "--one-line", action="store_true",
        help="1行サマリーで出力（heartbeat/監視用）"
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

    if args.one_line:
        # heartbeat/監視スクリプト向けの1行サマリー
        s = result["summary"]
        passed = result["passed_count"]
        total = result["total_checks"]
        status = "GRADUATED" if result["graduated"] else "NOT_YET"
        sharpe_check = next((c for c in result["checks"] if c["name"] == "ローリングSharpe"), None)
        sharpe_val = sharpe_check["value"] if sharpe_check else 0.0
        grad_strats = result.get("graduated_strategies", [])
        grad_str = ",".join(grad_strats) if grad_strats else "none"
        print(f"[{status}] {passed}/{total} | {s['total_trades']}trades | "
              f"PnL:{s['total_pnl_jpy']:+,.0f} | ROI:{s['roi_pct']:+.2f}% | "
              f"Sharpe:{sharpe_val:.3f} | grad:{grad_str}")
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

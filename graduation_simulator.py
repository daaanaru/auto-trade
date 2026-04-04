"""
graduation_simulator.py — 卒業軌道シミュレーション（モンテカルロ）

ペーパートレード残り日数でSharpe 0.5以上達成の可能性を確率で提示する。
過去決済履歴（closed_trades）を戦略別に分析し、ブートストラップ再サンプリングで
シミュレーション実行。卒業期限延長判断の意思決定支援ツール。

使い方:
  python3 graduation_simulator.py                # 標準出力（ASCII表示）
  python3 graduation_simulator.py --json         # JSON形式で出力
  python3 graduation_simulator.py --verbose      # 詳細ログ
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent

PORTFOLIO_FILE = PROJECT_ROOT / "paper_portfolio.json"
PERFORMANCE_LOG_FILE = PROJECT_ROOT / "paper_portfolio_log.json"


class TradeRecord(TypedDict, total=False):
    """closed_trades の型定義"""
    code: str
    strategy: str
    side: str
    entry_date: str
    close_date: str
    net_pnl_jpy: float
    exit_price: float


def load_json(path: Path) -> dict | list:
    """JSONを読み込む。ファイル不在なら空を返す。"""
    if not path.exists():
        return [] if "log" in path.name else {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return [] if "log" in path.name else {}


def _daily_values(perf_log: list) -> list[float]:
    """portfolio_logから各日の最終レコードを抽出（日中ノイズ除去）。"""
    daily = {}
    for r in perf_log:
        if not r.get("total_value_jpy"):
            continue
        date_key = r["timestamp"][:10]
        daily[date_key] = r["total_value_jpy"]
    return [v for _, v in sorted(daily.items())]


def calculate_sharpe(values: list[float]) -> float:
    """日次値から年率Sharpe比を計算（√252年率換算）。"""
    if len(values) < 5:
        return 0.0

    returns = pd.Series(values).pct_change().dropna()
    if returns.std() == 0:
        return 0.0

    return float((returns.mean() / returns.std()) * np.sqrt(252))


def analyze_current_state(portfolio: dict, perf_log: list) -> dict:
    """現在の成績を分析。"""
    values = _daily_values(perf_log)
    closed_trades = portfolio.get("closed_trades", [])
    capital = portfolio.get("initial_capital_jpy", 300000)

    total_pnl = sum(t.get("net_pnl_jpy", 0) for t in closed_trades)
    wins = sum(1 for t in closed_trades if t.get("net_pnl_jpy", 0) > 0)
    total_trades = len(closed_trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    sharpe = calculate_sharpe(values)

    # 最終資産（cash + ポジション含み）
    current_cash = portfolio.get("cash_jpy", capital)
    positions = portfolio.get("positions", [])
    # 簡略: current_value = cash + sum(position market values)
    # 正確には各ポジションの現在価格が必要だが、perf_logの最終total_value_jpyを使用
    current_value = values[-1] if values else capital

    return {
        "capital_jpy": capital,
        "current_value_jpy": current_value,
        "total_pnl_jpy": total_pnl,
        "win_rate_pct": win_rate,
        "sharpe": sharpe,
        "total_trades": total_trades,
        "current_cash_jpy": current_cash,
    }


def analyze_strategies(portfolio: dict) -> dict[str, dict]:
    """戦略別の成績を分析。"""
    closed_trades = portfolio.get("closed_trades", [])

    strategies = {}
    for trade in closed_trades:
        strategy = trade.get("strategy", "unknown")
        if strategy not in strategies:
            strategies[strategy] = {
                "trades": [],
                "total_wins": 0,
                "total_losses": 0,
                "total_pnl": 0.0,
            }

        pnl = trade.get("net_pnl_jpy", 0)
        strategies[strategy]["trades"].append(trade)
        strategies[strategy]["total_pnl"] += pnl

        if pnl > 0:
            strategies[strategy]["total_wins"] += 1
        else:
            strategies[strategy]["total_losses"] += 1

    # 統計化
    for strategy_name, data in strategies.items():
        trades_list = data["trades"]
        pnls = [t.get("net_pnl_jpy", 0) for t in trades_list]

        total = len(trades_list)
        win_rate = (data["total_wins"] / total * 100) if total > 0 else 0.0
        avg_win = np.mean([p for p in pnls if p > 0]) if any(p > 0 for p in pnls) else 0.0
        avg_loss = np.mean([p for p in pnls if p < 0]) if any(p < 0 for p in pnls) else 0.0
        std_pnl = np.std(pnls) if len(pnls) > 1 else 0.0

        strategies[strategy_name].update({
            "count": total,
            "win_rate_pct": win_rate,
            "avg_win_jpy": avg_win,
            "avg_loss_jpy": avg_loss,
            "std_pnl_jpy": std_pnl,
        })

    return strategies


def calculate_days_remaining(portfolio: dict) -> int:
    """ペーパートレード開始日から残り日数を計算（4/12期限）。"""
    deadline = datetime(2026, 4, 12, 23, 59, 59)
    now = datetime.now()
    remaining = (deadline - now).days
    return max(0, remaining)


def simulate_graduation_path(
    portfolio: dict,
    perf_log: list,
    days_remaining: int,
    num_simulations: int = 1000,
    verbose: bool = False,
) -> dict:
    """モンテカルロシミュレーション: 残り日数でSharpe≥0.5達成確率を計算。

    Args:
        portfolio: paper_portfolio.json
        perf_log: paper_portfolio_log.json
        days_remaining: 残り日数
        num_simulations: シミュレーション回数
        verbose: 詳細ログ出力

    Returns:
        シミュレーション結果辞書
    """

    closed_trades = portfolio.get("closed_trades", [])
    if not closed_trades:
        return {
            "warning": "取引データが不足しています",
            "trades_count": 0,
            "probability_normal": 0.0,
            "probability_extended": 0.0,
            "probability_relaxed": 0.0,
        }

    # 現状分析
    current_state = analyze_current_state(portfolio, perf_log)
    strategies = analyze_strategies(portfolio)

    # 日次リターンを取得（ボラティリティ推定）
    values = _daily_values(perf_log)
    if len(values) < 5:
        daily_returns = []
    else:
        daily_returns = list(pd.Series(values).pct_change().dropna())

    # 決済PnLを集める
    pnls = [t.get("net_pnl_jpy", 0) for t in closed_trades]
    win_count = sum(1 for p in pnls if p > 0)
    win_rate = win_count / len(pnls) if pnls else 0.0

    # シナリオ定義
    scenarios = {
        "normal": {
            "name": "現状継続",
            "days": days_remaining,
            "win_rate_adj": 0.0,
            "pnl_adj": 0.0,
        },
        "extended": {
            "name": "2週間延長",
            "days": days_remaining + 14,
            "win_rate_adj": 0.0,
            "pnl_adj": 0.0,
        },
        "relaxed": {
            "name": "条件緩和(Sharpe→0.0)",
            "days": days_remaining,
            "win_rate_adj": 0.0,
            "pnl_adj": 0.0,
            "sharpe_target": 0.0,  # カスタムターゲット
        },
    }

    results = {}

    for scenario_key, scenario in scenarios.items():
        target_sharpe = scenario.get("sharpe_target", 0.5)
        sim_days = scenario["days"]

        if sim_days <= 0:
            results[scenario_key] = {
                "name": scenario["name"],
                "days": sim_days,
                "num_simulations": num_simulations,
                "probability_pct": 0.0,
                "reason": "残り日数がありません",
            }
            continue

        # シミュレーション: 残り日数分の日次リターンをブートストラップで生成し
        # 既存日次値に連結してSharpeを算出（全期間で評価）
        success_count = 0

        for sim_idx in range(num_simulations):
            # 既存の日次値をベースに、sim_days分の日次リターンをリサンプリング
            if daily_returns:
                sim_returns = np.random.choice(daily_returns, size=sim_days, replace=True)
                # 既存値の末尾から連続的にパスを生成
                last_value = values[-1] if values else current_state["current_value_jpy"]
                sim_daily = [last_value]
                for ret in sim_returns:
                    sim_daily.append(sim_daily[-1] * (1 + ret))
                # 既存日次値 + シミュレーション値を連結してSharpe算出
                full_values = values + sim_daily[1:]
            else:
                # リターンデータ不足: PnL分布から簡易推定
                current_value = current_state["current_value_jpy"]
                if pnls:
                    sim_pnls = np.random.choice(pnls, size=sim_days * 6, replace=True)
                    daily_pnl = [sum(sim_pnls[i*6:(i+1)*6]) for i in range(sim_days)]
                else:
                    daily_pnl = [0.0] * sim_days
                sim_daily = [current_value]
                for dp in daily_pnl:
                    sim_daily.append(sim_daily[-1] + dp)
                full_values = values + sim_daily[1:] if values else sim_daily

            # 全期間のSharpeを計算（既存+シミュレーション）
            sim_sharpe = calculate_sharpe(full_values)

            if sim_sharpe >= target_sharpe:
                success_count += 1

            if verbose and sim_idx < 3:
                print(f"  [Sim {sim_idx}] Sharpe={sim_sharpe:.3f} / Target={target_sharpe}", file=sys.stderr)

        probability = (success_count / num_simulations * 100) if num_simulations > 0 else 0.0
        results[scenario_key] = {
            "name": scenario["name"],
            "days": sim_days,
            "num_simulations": num_simulations,
            "probability_pct": probability,
            "target_sharpe": target_sharpe,
        }

    return {
        "current_state": current_state,
        "strategies": strategies,
        "days_remaining": days_remaining,
        "simulations": results,
    }


def format_report_text(sim_result: dict) -> str:
    """シミュレーション結果をテキスト形式で出力。"""
    current = sim_result["current_state"]
    days = sim_result["days_remaining"]
    sims = sim_result["simulations"]
    strategies = sim_result["strategies"]

    lines = []
    lines.append("")
    lines.append("═" * 70)
    lines.append("  卒業軌道シミュレーション — 残り日数での達成確率")
    lines.append("═" * 70)
    lines.append("")

    # 現在値
    lines.append(f"📊 現在値")
    lines.append(f"  Sharpe: {current['sharpe']:+.2f} (目標: ≥0.5)")
    lines.append(f"  勝率: {current['win_rate_pct']:.1f}%")
    lines.append(f"  PnL: {current['total_pnl_jpy']:+,.0f} JPY")
    lines.append(f"  資産: {current['current_value_jpy']:,.0f} JPY")
    lines.append(f"  取引数: {current['total_trades']}回")
    lines.append("")

    # 卒業期限
    lines.append(f"📅 卒業期限")
    lines.append(f"  残り日数: {days}日")
    lines.append("")

    # シミュレーション結果
    lines.append("🎯 卒業確率（Sharpe≥0.5）")
    for key, result in [("normal", sims["normal"]), ("extended", sims["extended"]), ("relaxed", sims["relaxed"])]:
        mark = "✓" if result["probability_pct"] > 20 else "✗"
        target_str = f"(≥{result.get('target_sharpe', 0.5):.1f})" if key == "relaxed" else ""
        lines.append(f"  {mark} {result['name']:<20} ({result['days']}日): {result['probability_pct']:5.1f}% {target_str}")
    lines.append("")

    # 戦略別貢献度
    if strategies:
        lines.append("📈 戦略別貢献度")
        sorted_strats = sorted(
            strategies.items(),
            key=lambda x: x[1]["total_pnl"],
            reverse=True
        )
        for strategy_name, data in sorted_strats:
            mark = "★" if data["total_pnl"] > 0 else "⚠"
            lines.append(
                f"  {mark} {strategy_name:<20} "
                f"{data['total_pnl']:+7,.0f} JPY "
                f"(勝率 {data['win_rate_pct']:5.1f}% / {data['count']}件)"
            )
        lines.append("")

    lines.append("=" * 70)
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="卒業軌道シミュレーション — 残り日数でのSharpe 0.5達成確率を計算"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="結果をJSON形式で出力"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="詳細ログを表示"
    )
    args = parser.parse_args()

    # データ読み込み
    portfolio = load_json(PORTFOLIO_FILE)
    perf_log = load_json(PERFORMANCE_LOG_FILE)

    if not isinstance(portfolio, dict):
        portfolio = {}
    if not isinstance(perf_log, list):
        perf_log = []

    # 卒業期限までの残り日数を計算
    days_remaining = calculate_days_remaining(portfolio)

    # シミュレーション実行
    sim_result = simulate_graduation_path(
        portfolio, perf_log, days_remaining,
        num_simulations=1000, verbose=args.verbose
    )

    if args.json:
        # JSON出力（heartbeat連携用）
        output = {
            "timestamp": datetime.now().isoformat(),
            "days_remaining": days_remaining,
            "current_state": sim_result["current_state"],
            "strategies": sim_result["strategies"],
            "simulations": sim_result["simulations"],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        # テキスト出力（標準）
        print(format_report_text(sim_result))

    sys.exit(0)


if __name__ == "__main__":
    main()

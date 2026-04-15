#!/usr/bin/env python3
"""
戦略別パフォーマンス分析ツール

paper_portfolio.json の closed_trades を戦略別に集計し、
以下を提供する:
- 戦略別集計: 件数、勝率(%)、平均損益(円)、合計損益(円)、平均保有期間(時間)
- 市場×戦略マトリクス: 各セルに合計損益
- 直近7日 vs 全期間: 勝率・平均損益のモメンタム比較
- 最悪トレードTOP5（損失額順）

CLI引数:
  --json: JSON形式で出力
  --output FILE: ファイルに保存（デフォルト: 標準出力）
  --portfolio FILE: ポートフォリオファイル（デフォルト: paper_portfolio.json）
"""

import json
import sys
import os
from datetime import datetime, timedelta
from statistics import mean, stdev
from argparse import ArgumentParser


def load_portfolio(filepath):
    """ポートフォリオファイルを読み込む（読み取り専用）"""
    with open(filepath, 'r') as f:
        data = json.load(f)
    return data.get('closed_trades', [])


def parse_date(date_str):
    """ISO形式の日付文字列をdatetimeオブジェクトに変換"""
    return datetime.fromisoformat(date_str.replace('Z', '+00:00'))


def calculate_holding_hours(entry_date_str, exit_date_str):
    """エントリー日時から出口日時までの時間を計算"""
    entry = parse_date(entry_date_str)
    exit_dt = parse_date(exit_date_str)
    delta = exit_dt - entry
    return delta.total_seconds() / 3600


def is_profitable(net_pnl):
    """損益がプラスかを判定"""
    return net_pnl > 0


def aggregate_by_strategy(closed_trades):
    """戦略別に集計"""
    by_strategy = {}

    for trade in closed_trades:
        strategy = trade.get('strategy', 'unknown')
        if strategy not in by_strategy:
            by_strategy[strategy] = {
                'trades': [],
                'count': 0,
                'wins': 0,
                'losses': 0,
                'total_pnl': 0.0,
                'holding_hours': [],
                'pnls': []
            }

        by_strategy[strategy]['trades'].append(trade)
        by_strategy[strategy]['count'] += 1
        by_strategy[strategy]['total_pnl'] += trade['net_pnl_jpy']
        by_strategy[strategy]['pnls'].append(trade['net_pnl_jpy'])
        by_strategy[strategy]['holding_hours'].append(
            calculate_holding_hours(trade['entry_date'], trade['exit_date'])
        )

        if is_profitable(trade['net_pnl_jpy']):
            by_strategy[strategy]['wins'] += 1
        else:
            by_strategy[strategy]['losses'] += 1

    # 統計量を計算
    for strategy in by_strategy:
        data = by_strategy[strategy]
        data['win_rate'] = (data['wins'] / data['count'] * 100) if data['count'] > 0 else 0.0
        data['avg_pnl'] = mean(data['pnls']) if data['pnls'] else 0.0
        data['avg_holding_hours'] = mean(data['holding_hours']) if data['holding_hours'] else 0.0

        # 標準偏差（2件以上の場合のみ）
        if len(data['pnls']) > 1:
            data['pnl_stdev'] = stdev(data['pnls'])
        else:
            data['pnl_stdev'] = 0.0

        # Sharpe比（簡易版: avg_pnl / pnl_stdev、stdev=0 または件数1の場合は None）
        if data['pnl_stdev'] > 0:
            data['sharpe_ratio'] = data['avg_pnl'] / data['pnl_stdev']
        else:
            data['sharpe_ratio'] = None

    return by_strategy


def market_strategy_matrix(closed_trades):
    """市場×戦略マトリクスを生成"""
    matrix = {}

    for trade in closed_trades:
        market = trade.get('market', 'unknown')
        strategy = trade.get('strategy', 'unknown')
        key = (market, strategy)

        if key not in matrix:
            matrix[key] = {
                'count': 0,
                'total_pnl': 0.0,
                'pnls': [],
                'wins': 0
            }

        matrix[key]['count'] += 1
        matrix[key]['total_pnl'] += trade['net_pnl_jpy']
        matrix[key]['pnls'].append(trade['net_pnl_jpy'])
        if is_profitable(trade['net_pnl_jpy']):
            matrix[key]['wins'] += 1

    # 勝率を追加
    for key in matrix:
        count = matrix[key]['count']
        matrix[key]['win_rate'] = (matrix[key]['wins'] / count * 100) if count > 0 else 0.0

    return matrix


def momentum_analysis(closed_trades):
    """直近7日 vs 全期間の勝率・平均損益を比較"""
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    recent = [t for t in closed_trades
              if parse_date(t['exit_date']) >= week_ago]
    all_trades = closed_trades

    def calc_metrics(trades):
        if not trades:
            return {'count': 0, 'win_rate': 0.0, 'avg_pnl': 0.0}

        wins = sum(1 for t in trades if is_profitable(t['net_pnl_jpy']))
        pnls = [t['net_pnl_jpy'] for t in trades]

        return {
            'count': len(trades),
            'win_rate': (wins / len(trades) * 100) if trades else 0.0,
            'avg_pnl': mean(pnls) if pnls else 0.0,
            'total_pnl': sum(pnls) if pnls else 0.0
        }

    return {
        'recent_7d': calc_metrics(recent),
        'all_time': calc_metrics(all_trades)
    }


def worst_trades(closed_trades, top_n=5):
    """最悪トレードTOP5（損失額順）"""
    sorted_trades = sorted(
        closed_trades,
        key=lambda t: t['net_pnl_jpy']
    )

    result = []
    for trade in sorted_trades[:top_n]:
        result.append({
            'code': trade['code'],
            'name': trade['name'],
            'market': trade['market'],
            'strategy': trade['strategy'],
            'net_pnl_jpy': trade['net_pnl_jpy'],
            'entry_date': trade['entry_date'],
            'exit_date': trade['exit_date'],
            'holding_hours': calculate_holding_hours(trade['entry_date'], trade['exit_date']),
            'reason': trade.get('reason', 'unknown')
        })

    return result


def format_text_report(by_strategy, matrix, momentum, worst):
    """テキスト形式のレポートを生成"""
    lines = []

    # ヘッダー
    lines.append("=" * 80)
    lines.append("戦略別パフォーマンス分析")
    lines.append(f"生成時刻: {datetime.now().isoformat()}")
    lines.append("=" * 80)
    lines.append("")

    # セクション1: 戦略別集計
    lines.append("【戦略別集計】")
    lines.append("-" * 80)
    lines.append(f"{'戦略':<20} {'件数':>6} {'勝率':>8} {'平均損益':>12} {'合計損益':>12} {'平均保有時間':>12} {'Sharpe':>8}")
    lines.append("-" * 90)

    for strategy in sorted(by_strategy.keys()):
        data = by_strategy[strategy]
        sharpe_str = f"{data['sharpe_ratio']:>7.2f}" if data['sharpe_ratio'] is not None else "    N/A"
        lines.append(
            f"{strategy:<20} {data['count']:>6} {data['win_rate']:>7.1f}% "
            f"{data['avg_pnl']:>11.2f}円 {data['total_pnl']:>11.2f}円 {data['avg_holding_hours']:>11.1f}h {sharpe_str}"
        )

    lines.append("")

    # セクション2: 市場×戦略マトリクス
    lines.append("【市場×戦略マトリクス（合計損益）】")
    lines.append("-" * 80)

    # マトリクスの構造を作る
    markets = sorted(set(k[0] for k in matrix.keys()))
    strategies = sorted(set(k[1] for k in matrix.keys()))

    header = "市場\\戦略" + "".join(f"{s:>14}" for s in strategies)
    lines.append(header)
    lines.append("-" * len(header))

    for market in markets:
        row = f"{market:<12}"
        for strategy in strategies:
            key = (market, strategy)
            if key in matrix:
                pnl = matrix[key]['total_pnl']
                row += f" {pnl:>13.2f}円"
            else:
                row += "        ----"
        lines.append(row)

    lines.append("")

    # セクション3: モメンタム分析
    lines.append("【直近7日 vs 全期間の比較】")
    lines.append("-" * 80)

    recent = momentum['recent_7d']
    alltime = momentum['all_time']

    lines.append(f"直近7日:  {recent['count']:>4}件 / 勝率 {recent['win_rate']:>6.1f}% / 平均損益 {recent['avg_pnl']:>10.2f}円 / 合計損益 {recent['total_pnl']:>10.2f}円")
    lines.append(f"全期間:   {alltime['count']:>4}件 / 勝率 {alltime['win_rate']:>6.1f}% / 平均損益 {alltime['avg_pnl']:>10.2f}円 / 合計損益 {alltime['total_pnl']:>10.2f}円")

    lines.append("")

    # セクション4: 最悪トレードTOP5
    lines.append("【最悪トレードTOP5（損失額順）】")
    lines.append("-" * 80)

    for i, trade in enumerate(worst, 1):
        lines.append(
            f"{i}. {trade['code']:>10} ({trade['name']:<8}) "
            f"[{trade['market']:<6}] {trade['strategy']:<20} "
            f"損失: {trade['net_pnl_jpy']:>10.2f}円 / 保有: {trade['holding_hours']:>6.1f}h"
        )

    lines.append("")
    lines.append("=" * 80)

    return "\n".join(lines)


def format_json_report(by_strategy, matrix, momentum, worst):
    """JSON形式のレポートを生成"""
    # 戦略別データの整形（不要なキーを削除）
    strategy_data = {}
    for strategy, data in by_strategy.items():
        strategy_data[strategy] = {
            'count': data['count'],
            'wins': data['wins'],
            'losses': data['losses'],
            'win_rate': round(data['win_rate'], 2),
            'total_pnl': round(data['total_pnl'], 2),
            'avg_pnl': round(data['avg_pnl'], 2),
            'pnl_stdev': round(data['pnl_stdev'], 2),
            'avg_holding_hours': round(data['avg_holding_hours'], 2),
            'sharpe_ratio': round(data['sharpe_ratio'], 3) if data['sharpe_ratio'] is not None else None
        }

    # マトリクスの整形
    matrix_data = {}
    for (market, strategy), data in matrix.items():
        key = f"{market}_{strategy}"
        matrix_data[key] = {
            'market': market,
            'strategy': strategy,
            'count': data['count'],
            'wins': data['wins'],
            'win_rate': round(data['win_rate'], 2),
            'total_pnl': round(data['total_pnl'], 2)
        }

    # モメンタム分析の整形
    momentum_data = {
        'recent_7d': {
            'count': momentum['recent_7d']['count'],
            'win_rate': round(momentum['recent_7d']['win_rate'], 2),
            'avg_pnl': round(momentum['recent_7d']['avg_pnl'], 2),
            'total_pnl': round(momentum['recent_7d']['total_pnl'], 2)
        },
        'all_time': {
            'count': momentum['all_time']['count'],
            'win_rate': round(momentum['all_time']['win_rate'], 2),
            'avg_pnl': round(momentum['all_time']['avg_pnl'], 2),
            'total_pnl': round(momentum['all_time']['total_pnl'], 2)
        }
    }

    # 最悪トレードの整形
    worst_data = []
    for trade in worst:
        worst_data.append({
            'rank': len(worst_data) + 1,
            'code': trade['code'],
            'name': trade['name'],
            'market': trade['market'],
            'strategy': trade['strategy'],
            'net_pnl_jpy': round(trade['net_pnl_jpy'], 2),
            'entry_date': trade['entry_date'],
            'exit_date': trade['exit_date'],
            'holding_hours': round(trade['holding_hours'], 2),
            'reason': trade['reason']
        })

    return {
        'generated_at': datetime.now().isoformat(),
        'strategy_summary': strategy_data,
        'market_strategy_matrix': matrix_data,
        'momentum_analysis': momentum_data,
        'worst_trades': worst_data
    }


def main():
    parser = ArgumentParser(description='戦略別パフォーマンス分析ツール')
    parser.add_argument('--json', action='store_true', help='JSON形式で出力')
    parser.add_argument('--output', type=str, help='出力ファイルパス')
    parser.add_argument('--portfolio', type=str, default='paper_portfolio.json',
                       help='ポートフォリオファイル（デフォルト: paper_portfolio.json）')

    args = parser.parse_args()

    # ポートフォリオを読み込む
    if not os.path.exists(args.portfolio):
        print(f"エラー: {args.portfolio} が見つかりません", file=sys.stderr)
        sys.exit(1)

    closed_trades = load_portfolio(args.portfolio)

    if not closed_trades:
        print("警告: 決済済みトレードがありません", file=sys.stderr)
        closed_trades = []

    # 各分析を実行
    by_strategy = aggregate_by_strategy(closed_trades)
    matrix = market_strategy_matrix(closed_trades)
    momentum = momentum_analysis(closed_trades)
    worst = worst_trades(closed_trades, top_n=5)

    # 出力を生成
    if args.json:
        output = json.dumps(format_json_report(by_strategy, matrix, momentum, worst),
                          ensure_ascii=False, indent=2)
    else:
        output = format_text_report(by_strategy, matrix, momentum, worst)

    # 出力先に書き込み
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"出力をファイルに保存しました: {args.output}")
    else:
        print(output)


if __name__ == '__main__':
    main()

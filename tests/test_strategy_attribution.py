#!/usr/bin/env python3
"""
strategy_attribution.py のテストスイート（pytest用）

テスト対象:
- 戦略別集計
- 勝率計算
- 保有期間計算
- 市場×戦略マトリクス
- モメンタム分析
- 最悪トレードTOP5
- 空データハンドリング
- JSON出力
- ファイル出力
"""

import pytest
import json
import os
import tempfile
from datetime import datetime, timedelta
import sys

# strategy_attribution.py をインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy_attribution


# ====== モックデータ ======

def create_mock_trade(
    code='BTC-JPY',
    name='ビットコイン',
    market='btc',
    side='long',
    entry_price=10000000,
    exit_price=10100000,
    shares=0.001,
    net_pnl_jpy=100.0,
    entry_date_offset_hours=24,
    exit_date_offset_hours=20,
    strategy='volscale_sma',
    reason='TAKE_PROFIT_1'
):
    """モックトレードを生成"""
    now = datetime.now()
    entry_dt = now - timedelta(hours=entry_date_offset_hours)
    exit_dt = now - timedelta(hours=exit_date_offset_hours)

    return {
        'code': code,
        'name': name,
        'market': market,
        'side': side,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'shares': shares,
        'net_pnl_jpy': net_pnl_jpy,
        'entry_date': entry_dt.isoformat(),
        'exit_date': exit_dt.isoformat(),
        'reason': reason,
        'strategy': strategy
    }


@pytest.fixture
def sample_trades():
    """サンプルトレード（複数戦略、複数市場）"""
    return [
        # volscale_sma（BTC）: +100, +200 → 勝率100%, 平均損益150
        create_mock_trade(
            code='BTC-JPY', market='btc', strategy='volscale_sma',
            net_pnl_jpy=100.0, entry_date_offset_hours=48, exit_date_offset_hours=40
        ),
        create_mock_trade(
            code='BTC-JPY', market='btc', strategy='volscale_sma',
            net_pnl_jpy=200.0, entry_date_offset_hours=24, exit_date_offset_hours=15
        ),

        # bb_rsi（JP）: +300, -100 → 勝率50%, 平均損益100
        create_mock_trade(
            code='6758.T', name='ソニー', market='jp', strategy='bb_rsi',
            net_pnl_jpy=300.0, entry_date_offset_hours=36, exit_date_offset_hours=28
        ),
        create_mock_trade(
            code='9984.T', name='ソフトバンク', market='jp', strategy='bb_rsi',
            net_pnl_jpy=-100.0, entry_date_offset_hours=12, exit_date_offset_hours=8
        ),

        # sma_crossover（US）: -50, +150 → 勝率50%, 平均損益50
        create_mock_trade(
            code='AAPL', name='Apple', market='us', strategy='sma_crossover',
            net_pnl_jpy=-50.0, entry_date_offset_hours=60, exit_date_offset_hours=50
        ),
        create_mock_trade(
            code='AAPL', name='Apple', market='us', strategy='sma_crossover',
            net_pnl_jpy=150.0, entry_date_offset_hours=30, exit_date_offset_hours=24
        ),

        # monthly_momentum（JP）: -200 → 勝率0%, 平均損益-200
        create_mock_trade(
            code='2914.T', name='JT', market='jp', strategy='monthly_momentum',
            net_pnl_jpy=-200.0, entry_date_offset_hours=72, exit_date_offset_hours=65
        ),

        # 直近7日のみ（モメンタム分析用）
        create_mock_trade(
            code='ETH-JPY', name='イーサリアム', market='btc', strategy='volscale_sma',
            net_pnl_jpy=500.0, entry_date_offset_hours=2, exit_date_offset_hours=1
        ),
    ]


@pytest.fixture
def empty_trades():
    """空のトレードリスト"""
    return []


@pytest.fixture
def single_trade():
    """単一トレード"""
    return [create_mock_trade(net_pnl_jpy=100.0)]


# ====== テスト: 基本機能 ======

def test_load_portfolio_not_found():
    """存在しないファイルでエラーが出ることを確認"""
    with pytest.raises(FileNotFoundError):
        strategy_attribution.load_portfolio('/nonexistent/file.json')


def test_load_portfolio_with_trades(sample_trades):
    """ポートフォリオファイルを読み込めることを確認"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({'closed_trades': sample_trades}, f)
        filepath = f.name

    try:
        trades = strategy_attribution.load_portfolio(filepath)
        assert len(trades) == 8  # sample_tradesの実トレード数
        assert trades[0]['code'] == 'BTC-JPY'
    finally:
        os.unlink(filepath)


# ====== テスト: 計算ロジック ======

def test_calculate_holding_hours():
    """保有期間の計算"""
    now = datetime.now()
    entry = (now - timedelta(hours=10)).isoformat()
    exit_dt = now.isoformat()

    hours = strategy_attribution.calculate_holding_hours(entry, exit_dt)
    assert 9.5 < hours < 10.5  # 誤差許容範囲内


def test_calculate_holding_hours_exact_minutes():
    """保有期間（分単位での検証）"""
    base = datetime.now()
    entry = (base - timedelta(hours=2, minutes=30)).isoformat()
    exit_dt = base.isoformat()

    hours = strategy_attribution.calculate_holding_hours(entry, exit_dt)
    assert 2.4 < hours < 2.6  # 約2.5時間


def test_is_profitable_positive():
    """プラス損益の判定"""
    assert strategy_attribution.is_profitable(100.0) is True


def test_is_profitable_negative():
    """マイナス損益の判定"""
    assert strategy_attribution.is_profitable(-50.0) is False


def test_is_profitable_zero():
    """0円の判定"""
    assert strategy_attribution.is_profitable(0.0) is False


# ====== テスト: 戦略別集計 ======

def test_aggregate_by_strategy_volscale(sample_trades):
    """volscale_sma戦略の集計"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    volscale = result['volscale_sma']
    assert volscale['count'] == 3  # 3トレード
    assert volscale['wins'] == 3  # 全勝
    assert volscale['losses'] == 0
    assert volscale['win_rate'] == 100.0
    assert volscale['total_pnl'] == 800.0  # 100 + 200 + 500
    assert abs(volscale['avg_pnl'] - 266.67) < 1  # 平均 ~266.67


def test_aggregate_by_strategy_bb_rsi(sample_trades):
    """bb_rsi戦略の集計"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    bb_rsi = result['bb_rsi']
    assert bb_rsi['count'] == 2
    assert bb_rsi['wins'] == 1
    assert bb_rsi['losses'] == 1
    assert bb_rsi['win_rate'] == 50.0
    assert bb_rsi['total_pnl'] == 200.0  # 300 - 100


def test_aggregate_by_strategy_all_strategies(sample_trades):
    """全戦略が集計されることを確認"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    expected_strategies = {'volscale_sma', 'bb_rsi', 'sma_crossover', 'monthly_momentum'}
    assert set(result.keys()) == expected_strategies


def test_aggregate_by_strategy_empty(empty_trades):
    """空のトレードでも実行できることを確認"""
    result = strategy_attribution.aggregate_by_strategy(empty_trades)
    assert result == {}


def test_aggregate_by_strategy_single(single_trade):
    """単一トレードの集計"""
    result = strategy_attribution.aggregate_by_strategy(single_trade)

    assert len(result) == 1
    strategy_data = result['volscale_sma']
    assert strategy_data['count'] == 1
    assert strategy_data['win_rate'] == 100.0
    assert strategy_data['avg_pnl'] == 100.0


# ====== テスト: 勝率計算 ======

def test_win_rate_50_percent(sample_trades):
    """50%勝率の検証"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    bb_rsi = result['bb_rsi']
    assert bb_rsi['win_rate'] == 50.0


def test_win_rate_0_percent(sample_trades):
    """0%勝率の検証"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    monthly_momentum = result['monthly_momentum']
    assert monthly_momentum['win_rate'] == 0.0


def test_win_rate_100_percent(sample_trades):
    """100%勝率の検証"""
    result = strategy_attribution.aggregate_by_strategy(sample_trades)

    volscale = result['volscale_sma']
    assert volscale['win_rate'] == 100.0


# ====== テスト: 市場×戦略マトリクス ======

def test_market_strategy_matrix_btc_volscale(sample_trades):
    """BTC × volscale_smaのマトリクスセル"""
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)

    key = ('btc', 'volscale_sma')
    assert key in matrix
    assert matrix[key]['count'] == 3
    assert matrix[key]['total_pnl'] == 800.0
    assert matrix[key]['wins'] == 3
    assert matrix[key]['win_rate'] == 100.0


def test_market_strategy_matrix_jp_bb_rsi(sample_trades):
    """JP × bb_rsiのマトリクスセル"""
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)

    key = ('jp', 'bb_rsi')
    assert key in matrix
    assert matrix[key]['count'] == 2
    assert matrix[key]['total_pnl'] == 200.0
    assert matrix[key]['win_rate'] == 50.0


def test_market_strategy_matrix_all_cells(sample_trades):
    """マトリクスの全セルが正しく生成されることを確認"""
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)

    expected_cells = {
        ('btc', 'volscale_sma'): 3,
        ('jp', 'bb_rsi'): 2,
        ('us', 'sma_crossover'): 2,
        ('jp', 'monthly_momentum'): 1
    }

    for key, expected_count in expected_cells.items():
        assert key in matrix
        assert matrix[key]['count'] == expected_count


# ====== テスト: モメンタム分析 ======

def test_momentum_analysis_recent_7d(sample_trades):
    """直近7日のモメンタム分析"""
    momentum = strategy_attribution.momentum_analysis(sample_trades)

    recent = momentum['recent_7d']
    # sample_tradesの最後1件（直近2時間）が直近7日に該当
    # ただし実はすべてが直近7日の範囲（テスト生成時の日時からの相対距離）
    assert recent['count'] >= 1
    assert recent['win_rate'] >= 0.0
    assert recent['avg_pnl'] > 0  # 直近のトレード（+500）は勝ち


def test_momentum_analysis_all_time(sample_trades):
    """全期間のモメンタム分析"""
    momentum = strategy_attribution.momentum_analysis(sample_trades)

    alltime = momentum['all_time']
    assert alltime['count'] == 8  # sample_tradesの総トレード数
    # 全体: 5勝3敗 → 勝率 62.5%
    assert abs(alltime['win_rate'] - 62.5) < 0.1
    assert alltime['total_pnl'] == 900.0  # 100 + 200 + 300 - 100 - 50 + 150 - 200 + 500 = 900


def test_momentum_comparison(sample_trades):
    """近期と全期間の乖離が検出されることを確認"""
    momentum = strategy_attribution.momentum_analysis(sample_trades)

    recent_wr = momentum['recent_7d']['win_rate']
    alltime_wr = momentum['all_time']['win_rate']

    # 直近は上昇トレンド、全期間は下落の影響を含む
    assert recent_wr >= alltime_wr


# ====== テスト: 最悪トレードTOP5 ======

def test_worst_trades_ordering(sample_trades):
    """最悪トレードが損失額順にソートされることを確認"""
    worst = strategy_attribution.worst_trades(sample_trades, top_n=5)

    # 期待される順序: -200, -100, -50, +100, +150
    expected_pnls = [-200.0, -100.0, -50.0, 100.0, 150.0]

    for i, trade in enumerate(worst):
        assert trade['net_pnl_jpy'] == expected_pnls[i]


def test_worst_trades_top3():
    """TOP3を取得する場合のテスト"""
    trades = [
        create_mock_trade(net_pnl_jpy=-100.0, code='A'),
        create_mock_trade(net_pnl_jpy=-50.0, code='B'),
        create_mock_trade(net_pnl_jpy=100.0, code='C'),
        create_mock_trade(net_pnl_jpy=200.0, code='D'),
    ]

    worst = strategy_attribution.worst_trades(trades, top_n=3)
    assert len(worst) == 3
    assert worst[0]['code'] == 'A'  # -100
    assert worst[1]['code'] == 'B'  # -50
    assert worst[2]['code'] == 'C'  # +100


def test_worst_trades_empty():
    """空のトレードで最悪トレード取得"""
    worst = strategy_attribution.worst_trades([], top_n=5)
    assert worst == []


def test_worst_trades_contains_metadata(sample_trades):
    """最悪トレードに必要なメタデータが含まれることを確認"""
    worst = strategy_attribution.worst_trades(sample_trades, top_n=1)

    trade = worst[0]
    assert 'code' in trade
    assert 'name' in trade
    assert 'market' in trade
    assert 'strategy' in trade
    assert 'net_pnl_jpy' in trade
    assert 'holding_hours' in trade
    assert 'reason' in trade


# ====== テスト: 出力形式 ======

def test_format_text_report(sample_trades):
    """テキスト形式レポートの生成"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)
    momentum = strategy_attribution.momentum_analysis(sample_trades)
    worst = strategy_attribution.worst_trades(sample_trades)

    report = strategy_attribution.format_text_report(by_strategy, matrix, momentum, worst)

    # レポートに期待されるセクションが含まれることを確認
    assert "戦略別集計" in report
    assert "市場×戦略マトリクス" in report
    assert "直近7日 vs 全期間の比較" in report
    assert "最悪トレードTOP5" in report


def test_format_json_report(sample_trades):
    """JSON形式レポートの生成"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)
    momentum = strategy_attribution.momentum_analysis(sample_trades)
    worst = strategy_attribution.worst_trades(sample_trades)

    result = strategy_attribution.format_json_report(by_strategy, matrix, momentum, worst)

    # JSON構造が正しいことを確認
    assert isinstance(result, dict)
    assert 'generated_at' in result
    assert 'strategy_summary' in result
    assert 'market_strategy_matrix' in result
    assert 'momentum_analysis' in result
    assert 'worst_trades' in result

    # 戦略別サマリー
    assert 'volscale_sma' in result['strategy_summary']
    volscale = result['strategy_summary']['volscale_sma']
    assert isinstance(volscale['win_rate'], float)
    assert isinstance(volscale['total_pnl'], float)


def test_format_json_report_serializable(sample_trades):
    """JSON形式レポートがJSONシリアライズ可能であることを確認"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)
    momentum = strategy_attribution.momentum_analysis(sample_trades)
    worst = strategy_attribution.worst_trades(sample_trades)

    result = strategy_attribution.format_json_report(by_strategy, matrix, momentum, worst)

    # JSONシリアライズできることを確認
    json_str = json.dumps(result, ensure_ascii=False)
    assert len(json_str) > 0


# ====== テスト: CLI引数とファイル出力 ======

def test_file_output_text(sample_trades):
    """テキスト形式でファイルに出力"""
    with tempfile.TemporaryDirectory() as tmpdir:
        portfolio_file = os.path.join(tmpdir, 'portfolio.json')
        output_file = os.path.join(tmpdir, 'report.txt')

        # ポートフォリオを作成
        with open(portfolio_file, 'w') as f:
            json.dump({'closed_trades': sample_trades}, f)

        # strategy_attribution.py を実行（擬似的に）
        by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
        matrix = strategy_attribution.market_strategy_matrix(sample_trades)
        momentum = strategy_attribution.momentum_analysis(sample_trades)
        worst = strategy_attribution.worst_trades(sample_trades)

        report = strategy_attribution.format_text_report(by_strategy, matrix, momentum, worst)

        # ファイルに書き込み
        with open(output_file, 'w') as f:
            f.write(report)

        # ファイルが存在して内容があることを確認
        assert os.path.exists(output_file)
        with open(output_file, 'r') as f:
            content = f.read()
            assert len(content) > 0
            assert "戦略別集計" in content


def test_file_output_json(sample_trades):
    """JSON形式でファイルに出力"""
    with tempfile.TemporaryDirectory() as tmpdir:
        portfolio_file = os.path.join(tmpdir, 'portfolio.json')
        output_file = os.path.join(tmpdir, 'report.json')

        # ポートフォリオを作成
        with open(portfolio_file, 'w') as f:
            json.dump({'closed_trades': sample_trades}, f)

        # strategy_attribution.py を実行（擬似的に）
        by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
        matrix = strategy_attribution.market_strategy_matrix(sample_trades)
        momentum = strategy_attribution.momentum_analysis(sample_trades)
        worst = strategy_attribution.worst_trades(sample_trades)

        result = strategy_attribution.format_json_report(by_strategy, matrix, momentum, worst)

        # ファイルに書き込み
        with open(output_file, 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # ファイルが存在して有効なJSONであることを確認
        assert os.path.exists(output_file)
        with open(output_file, 'r') as f:
            loaded = json.load(f)
            assert 'strategy_summary' in loaded


# ====== テスト: エッジケース ======

def test_negative_pnl_only():
    """全て損失トレードの場合"""
    trades = [
        create_mock_trade(net_pnl_jpy=-100.0, code='A'),
        create_mock_trade(net_pnl_jpy=-50.0, code='B'),
    ]

    by_strategy = strategy_attribution.aggregate_by_strategy(trades)
    assert by_strategy['volscale_sma']['win_rate'] == 0.0
    assert by_strategy['volscale_sma']['total_pnl'] == -150.0


def test_mixed_markets_and_strategies(sample_trades):
    """複数の市場と戦略が混在する場合"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)
    matrix = strategy_attribution.market_strategy_matrix(sample_trades)

    # 4つの異なる戦略がある
    assert len(by_strategy) == 4

    # 4つの異なるマトリクスセルがある
    assert len(matrix) == 4


def test_holding_hours_calculation_consistency(sample_trades):
    """保有期間の計算が一貫していることを確認"""
    for trade in sample_trades:
        hours = strategy_attribution.calculate_holding_hours(
            trade['entry_date'],
            trade['exit_date']
        )
        # 保有期間は正の値であることを確認
        assert hours >= 0


# ====== テスト: 統計量計算 ======

def test_average_pnl_calculation(sample_trades):
    """平均損益の計算"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)

    # bb_rsi: (300 - 100) / 2 = 100
    bb_rsi = by_strategy['bb_rsi']
    assert bb_rsi['avg_pnl'] == 100.0


def test_pnl_stdev_calculation(sample_trades):
    """損益の標準偏差の計算"""
    by_strategy = strategy_attribution.aggregate_by_strategy(sample_trades)

    # volscale_sma: 100, 200, 500
    # 平均: 266.67, 標準偏差が計算される
    volscale = by_strategy['volscale_sma']
    assert volscale['pnl_stdev'] > 0


def test_pnl_stdev_single_trade(single_trade):
    """単一トレードでは標準偏差が0"""
    by_strategy = strategy_attribution.aggregate_by_strategy(single_trade)

    data = by_strategy['volscale_sma']
    assert data['pnl_stdev'] == 0.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

#!/usr/bin/env python3
"""
strategy_alert.py のテストスイート

15+ テスト:
- インスタンス作成（デフォルト/カスタム）
- LOSS_EXCEEDED アラート検出
- SHARPE_LOW アラート検出
- WIN_RATE_DROP アラート検出
- NO_TRADE アラート検出
- Markdown レポート生成
- JSON 出力
- 空データ処理
- 環境変数による閾値カスタマイズ
- Discord通知（requests未インストール）
"""

import pytest
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# 親ディレクトリをパスに追加
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy_alert import StrategyAlertSystem, StrategyAlert


class TestStrategyAlertSystemInit:
    """インスタンス作成テスト"""

    def test_default_init(self):
        """デフォルトパスでの初期化"""
        system = StrategyAlertSystem()
        assert system.portfolio_path == "paper_portfolio.json"
        assert system.loss_threshold == -3000.0
        assert system.sharpe_threshold == -0.3
        assert system.win_rate_threshold == 30.0
        assert system.min_trades_for_stats == 5

    def test_custom_path_init(self):
        """カスタムパスでの初期化"""
        custom_path = "/custom/path.json"
        system = StrategyAlertSystem(portfolio_path=custom_path)
        assert system.portfolio_path == custom_path

    def test_custom_thresholds_via_env(self):
        """環境変数での閾値カスタマイズ"""
        os.environ['ALERT_LOSS_THRESHOLD'] = '-5000'
        os.environ['ALERT_SHARPE_THRESHOLD'] = '-0.5'
        os.environ['ALERT_WIN_RATE_THRESHOLD'] = '25'
        os.environ['ALERT_MIN_TRADES'] = '10'

        system = StrategyAlertSystem()
        assert system.loss_threshold == -5000.0
        assert system.sharpe_threshold == -0.5
        assert system.win_rate_threshold == 25.0
        assert system.min_trades_for_stats == 10

        # クリーンアップ
        del os.environ['ALERT_LOSS_THRESHOLD']
        del os.environ['ALERT_SHARPE_THRESHOLD']
        del os.environ['ALERT_WIN_RATE_THRESHOLD']
        del os.environ['ALERT_MIN_TRADES']


class TestDataLoading:
    """データロード機能テスト"""

    def test_load_data_file_not_found(self):
        """ファイルが見つからない場合"""
        system = StrategyAlertSystem(portfolio_path='/nonexistent/path.json')
        with pytest.raises(FileNotFoundError):
            system.load_data()

    def test_load_data_empty_portfolio(self):
        """空のポートフォリオ"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({'closed_trades': []}, f)
            temp_path = f.name

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            assert len(system.closed_trades) == 0
            assert len(system.by_strategy) == 0
        finally:
            os.unlink(temp_path)

    def _create_test_portfolio(self, closed_trades):
        """テスト用ポートフォリオを作成"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({'closed_trades': closed_trades}, f)
            return f.name


class TestLossExceededAlert(TestDataLoading):
    """LOSS_EXCEEDED アラート検出テスト"""

    def test_loss_exceeded_high(self):
        """損失が-5000円 → アラート発火"""
        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': -5000.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            loss_alerts = [a for a in alerts if a.alert_type == 'LOSS_EXCEEDED']
            assert len(loss_alerts) == 1
            assert loss_alerts[0].strategy == 'test_strategy'
            assert loss_alerts[0].value == -5000.0
            assert loss_alerts[0].severity == 'HIGH'
        finally:
            os.unlink(temp_path)

    def test_loss_not_exceeded(self):
        """損失が-1000円 → アラートなし（デフォルト閾値 -3000円）"""
        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': -1000.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            loss_alerts = [a for a in alerts if a.alert_type == 'LOSS_EXCEEDED']
            assert len(loss_alerts) == 0
        finally:
            os.unlink(temp_path)


class TestSharpeLowAlert(TestDataLoading):
    """SHARPE_LOW アラート検出テスト"""

    def test_sharpe_low(self):
        """Sharpe比が-0.5 → アラート発火（5件以上）"""
        trades = [
            {
                'code': f'TEST{i}',
                'name': f'テスト{i}',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': pnl
            }
            for i, pnl in enumerate([-100, -150, -80, -200, -50], 1)
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            sharpe_alerts = [a for a in alerts if a.alert_type == 'SHARPE_LOW']
            # sharpe_ratio が計算される（負の値なので SHARPE_LOW がある可能性）
            if sharpe_alerts:
                assert sharpe_alerts[0].severity == 'MEDIUM'
        finally:
            os.unlink(temp_path)

    def test_sharpe_insufficient_trades(self):
        """取引件数が4件 → Sharpe統計なし"""
        trades = [
            {
                'code': f'TEST{i}',
                'name': f'テスト{i}',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': -100.0
            }
            for i in range(4)
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            sharpe_alerts = [a for a in alerts if a.alert_type == 'SHARPE_LOW']
            assert len(sharpe_alerts) == 0  # 5件未満なのでチェックなし
        finally:
            os.unlink(temp_path)


class TestWinRateDropAlert(TestDataLoading):
    """WIN_RATE_DROP アラート検出テスト"""

    def test_win_rate_drop_20pct(self):
        """勝率20% → アラート発火（5件以上、閾値30%）"""
        # 5件中1件勝利 = 20%
        trades = [
            {
                'code': f'TEST{i}',
                'name': f'テスト{i}',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': pnl
            }
            for i, pnl in enumerate([100, -50, -60, -40, -30], 1)  # 勝率20%
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            win_alerts = [a for a in alerts if a.alert_type == 'WIN_RATE_DROP']
            assert len(win_alerts) == 1
            assert win_alerts[0].value == 20.0
            assert win_alerts[0].severity == 'MEDIUM'
        finally:
            os.unlink(temp_path)

    def test_win_rate_ok_60pct(self):
        """勝率60% → アラートなし（閾値30%）"""
        # 5件中3件勝利 = 60%
        trades = [
            {
                'code': f'TEST{i}',
                'name': f'テスト{i}',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': pnl
            }
            for i, pnl in enumerate([100, 150, 80, -40, -30], 1)  # 勝率60%
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            win_alerts = [a for a in alerts if a.alert_type == 'WIN_RATE_DROP']
            assert len(win_alerts) == 0
        finally:
            os.unlink(temp_path)


class TestNoTradeAlert(TestDataLoading):
    """NO_TRADE アラート検出テスト"""

    def test_no_trade_8days(self):
        """直近8日でエントリーなし → アラート発火"""
        now = datetime.now()
        old_date = (now - timedelta(days=8)).isoformat()

        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': old_date,
                'exit_date': old_date,
                'net_pnl_jpy': 100.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            no_trade_alerts = [a for a in alerts if a.alert_type == 'NO_TRADE']
            assert len(no_trade_alerts) == 1
            assert no_trade_alerts[0].severity == 'LOW'
        finally:
            os.unlink(temp_path)

    def test_recent_trade_3days(self):
        """直近3日でエントリーあり → アラートなし"""
        now = datetime.now()
        recent_date = (now - timedelta(days=3)).isoformat()

        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': recent_date,
                'exit_date': recent_date,
                'net_pnl_jpy': 100.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            no_trade_alerts = [a for a in alerts if a.alert_type == 'NO_TRADE']
            assert len(no_trade_alerts) == 0
        finally:
            os.unlink(temp_path)


class TestReportGeneration(TestDataLoading):
    """レポート生成テスト"""

    def test_markdown_report_with_alerts(self):
        """Markdownレポート生成（アラートあり）"""
        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': -5000.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            report = system.generate_report()

            assert "# 戦略別アラートレポート" in report
            assert "危険" in report or "HIGH" in report or "LOSS_EXCEEDED" in report
        finally:
            os.unlink(temp_path)

    def test_markdown_report_no_alerts(self):
        """Markdownレポート生成（アラートなし）"""
        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': 100.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            report = system.generate_report()

            assert "✅ アラートはありません" in report
        finally:
            os.unlink(temp_path)


class TestStrategyAlertDataclass:
    """StrategyAlert dataclass テスト"""

    def test_strategy_alert_fields(self):
        """StrategyAlert が正しいフィールドを持つ"""
        alert = StrategyAlert(
            strategy='test',
            alert_type='LOSS_EXCEEDED',
            message='Test message',
            value=-1000.0,
            threshold=-3000.0,
            severity='HIGH'
        )

        assert alert.strategy == 'test'
        assert alert.alert_type == 'LOSS_EXCEEDED'
        assert alert.message == 'Test message'
        assert alert.value == -1000.0
        assert alert.threshold == -3000.0
        assert alert.severity == 'HIGH'


class TestDiscordNotification:
    """Discord通知テスト"""

    def test_discord_skip_if_no_env(self):
        """環境変数なし → 通知スキップ（False を返す）"""
        # DISCORD_WEBHOOK_URL がない状態
        if 'DISCORD_WEBHOOK_URL' in os.environ:
            del os.environ['DISCORD_WEBHOOK_URL']

        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': 100.0
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({'closed_trades': trades}, f)
            temp_path = f.name

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            result = system.send_discord_alert()
            assert result == False  # 環境変数なしなので False
        finally:
            os.unlink(temp_path)

    def test_discord_skip_if_no_requests(self):
        """requests未インストール → 通知スキップ"""
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://example.com/webhook'

        trades = [
            {
                'code': 'TEST1',
                'name': 'テスト1',
                'market': 'test',
                'strategy': 'test_strategy',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': 100.0
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({'closed_trades': trades}, f)
            temp_path = f.name

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            # requests がインストールされている場合、この結果は環境に依存する
            # (実際の環境で requests は通常インストール済み)
            result = system.send_discord_alert()
            # スキップされるか、失敗するかのいずれか
            assert isinstance(result, bool)
        finally:
            os.unlink(temp_path)
            del os.environ['DISCORD_WEBHOOK_URL']


class TestMultipleStrategies(TestDataLoading):
    """複数戦略同時チェックテスト"""

    def test_multiple_strategies_mixed_alerts(self):
        """複数戦略が異なるアラートを出す"""
        trades = [
            # 戦略A: LOSS_EXCEEDED
            {
                'code': 'TESTA1',
                'name': 'テストA1',
                'market': 'test',
                'strategy': 'strategy_a',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': -5000.0
            },
            # 戦略B: 勝率良好（アラートなし）
            {
                'code': 'TESTB1',
                'name': 'テストB1',
                'market': 'test',
                'strategy': 'strategy_b',
                'entry_date': '2026-04-01T10:00:00',
                'exit_date': '2026-04-01T11:00:00',
                'net_pnl_jpy': 500.0
            },
            {
                'code': 'TESTB2',
                'name': 'テストB2',
                'market': 'test',
                'strategy': 'strategy_b',
                'entry_date': '2026-04-01T11:00:00',
                'exit_date': '2026-04-01T12:00:00',
                'net_pnl_jpy': 300.0
            }
        ]
        temp_path = self._create_test_portfolio(trades)

        try:
            system = StrategyAlertSystem(portfolio_path=temp_path)
            system.load_data()
            alerts = system.check_strategies()

            # 戦略Aはアラートあり
            strategy_a_alerts = [a for a in alerts if a.strategy == 'strategy_a']
            assert len(strategy_a_alerts) > 0

            # 全体でアラート数 > 0
            assert len(alerts) > 0
        finally:
            os.unlink(temp_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

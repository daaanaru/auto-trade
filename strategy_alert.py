#!/usr/bin/env python3
"""
strategy_alert.py — 戦略別損失上限アラートシステム

paper_portfolio.json の closed_trades を戦略別に集計し、
以下のアラートを自動検出：
- LOSS_EXCEEDED: トータル損益が閾値以下
- SHARPE_LOW: Sharpe比が閾値以下（5件以上のみ）
- WIN_RATE_DROP: 勝率が閾値以下（5件以上のみ）
- NO_TRADE: 直近7日間でエントリーゼロ

観察・報告のみ。ポジション操作は一切行わない。

CLI:
  python3 strategy_alert.py                    # アラート確認
  python3 strategy_alert.py --report           # Markdown形式で出力
  python3 strategy_alert.py --json             # JSON形式で出力
"""

import json
import sys
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from statistics import mean, stdev
from typing import List, Dict, Optional
from argparse import ArgumentParser


@dataclass
class StrategyAlert:
    """戦略別アラート"""
    strategy: str
    alert_type: str  # "LOSS_EXCEEDED" / "SHARPE_LOW" / "WIN_RATE_DROP" / "NO_TRADE"
    message: str
    value: float  # 実際の値（損益額、Sharpe比、勝率など）
    threshold: float  # 閾値
    severity: str  # "HIGH" / "MEDIUM" / "LOW"


class StrategyAlertSystem:
    """戦略別損失上限アラートシステム"""

    def __init__(
        self,
        portfolio_log_path: Optional[str] = None,
        portfolio_path: Optional[str] = None
    ):
        """
        初期化

        Args:
            portfolio_log_path: ポートフォリオログファイルパス（非推奨）
            portfolio_path: ポートフォリオファイルパス（デフォルト: paper_portfolio.json）
        """
        self.portfolio_path = portfolio_path or "paper_portfolio.json"
        self.closed_trades = []
        self.by_strategy = {}

        # 閾値を環境変数からオーバーライド
        self.loss_threshold = float(os.environ.get(
            'ALERT_LOSS_THRESHOLD', -3000.0
        ))
        self.sharpe_threshold = float(os.environ.get(
            'ALERT_SHARPE_THRESHOLD', -0.3
        ))
        self.win_rate_threshold = float(os.environ.get(
            'ALERT_WIN_RATE_THRESHOLD', 30.0
        ))
        self.min_trades_for_stats = int(os.environ.get(
            'ALERT_MIN_TRADES', 5
        ))

    def load_data(self) -> None:
        """ポートフォリオファイルを読み込む"""
        if not os.path.exists(self.portfolio_path):
            raise FileNotFoundError(f"{self.portfolio_path} が見つかりません")

        with open(self.portfolio_path, 'r') as f:
            data = json.load(f)

        self.closed_trades = data.get('closed_trades', [])
        self._aggregate_by_strategy()

    def _aggregate_by_strategy(self) -> None:
        """戦略別に集計"""
        self.by_strategy = {}

        for trade in self.closed_trades:
            strategy = trade.get('strategy', 'unknown')
            if strategy not in self.by_strategy:
                self.by_strategy[strategy] = {
                    'trades': [],
                    'count': 0,
                    'wins': 0,
                    'total_pnl': 0.0,
                    'pnls': [],
                    'entry_dates': []
                }

            self.by_strategy[strategy]['trades'].append(trade)
            self.by_strategy[strategy]['count'] += 1
            self.by_strategy[strategy]['total_pnl'] += trade['net_pnl_jpy']
            self.by_strategy[strategy]['pnls'].append(trade['net_pnl_jpy'])
            self.by_strategy[strategy]['entry_dates'].append(
                trade['entry_date']
            )

            if trade['net_pnl_jpy'] > 0:
                self.by_strategy[strategy]['wins'] += 1

        # 統計量を計算
        for strategy in self.by_strategy:
            data = self.by_strategy[strategy]
            data['win_rate'] = (
                (data['wins'] / data['count'] * 100)
                if data['count'] > 0
                else 0.0
            )
            data['avg_pnl'] = mean(data['pnls']) if data['pnls'] else 0.0

            # Sharpe比（簡易版）
            if len(data['pnls']) > 1:
                pnl_stdev = stdev(data['pnls'])
                if pnl_stdev > 0:
                    data['sharpe_ratio'] = data['avg_pnl'] / pnl_stdev
                else:
                    data['sharpe_ratio'] = None
            else:
                data['sharpe_ratio'] = None

    def _get_latest_entry_date(self, strategy: str) -> Optional[datetime]:
        """戦略の最新エントリー日時を取得"""
        if strategy not in self.by_strategy:
            return None

        trades = self.by_strategy[strategy]['trades']
        if not trades:
            return None

        dates = [self._parse_iso_date(t['entry_date']) for t in trades]
        return max(dates) if dates else None

    def _parse_iso_date(self, date_str: str) -> datetime:
        """ISO形式の日付をパース"""
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))

    def check_strategies(self) -> List[StrategyAlert]:
        """全戦略をチェックしてアラートリストを返す"""
        if not self.by_strategy:
            return []

        alerts = []

        now = datetime.now()
        week_ago = now - timedelta(days=7)

        for strategy, data in self.by_strategy.items():
            # 1. LOSS_EXCEEDED
            if data['total_pnl'] <= self.loss_threshold:
                alerts.append(StrategyAlert(
                    strategy=strategy,
                    alert_type="LOSS_EXCEEDED",
                    message=f"{strategy}: 累計損益が閾値を超過（{data['total_pnl']:.2f}円 <= {self.loss_threshold:.2f}円）",
                    value=data['total_pnl'],
                    threshold=self.loss_threshold,
                    severity="HIGH"
                ))

            # 2. SHARPE_LOW（5件以上の場合のみ）
            if data['count'] >= self.min_trades_for_stats:
                if (data['sharpe_ratio'] is not None and
                    data['sharpe_ratio'] <= self.sharpe_threshold):
                    alerts.append(StrategyAlert(
                        strategy=strategy,
                        alert_type="SHARPE_LOW",
                        message=f"{strategy}: Sharpe比が低い（{data['sharpe_ratio']:.3f} <= {self.sharpe_threshold:.3f}）",
                        value=data['sharpe_ratio'],
                        threshold=self.sharpe_threshold,
                        severity="MEDIUM"
                    ))

            # 3. WIN_RATE_DROP（5件以上の場合のみ）
            if data['count'] >= self.min_trades_for_stats:
                if data['win_rate'] <= self.win_rate_threshold:
                    alerts.append(StrategyAlert(
                        strategy=strategy,
                        alert_type="WIN_RATE_DROP",
                        message=f"{strategy}: 勝率が低い（{data['win_rate']:.1f}% <= {self.win_rate_threshold:.1f}%）",
                        value=data['win_rate'],
                        threshold=self.win_rate_threshold,
                        severity="MEDIUM"
                    ))

            # 4. NO_TRADE（直近7日でエントリーゼロ）
            latest_entry = self._get_latest_entry_date(strategy)
            if latest_entry is None or latest_entry < week_ago:
                alerts.append(StrategyAlert(
                    strategy=strategy,
                    alert_type="NO_TRADE",
                    message=f"{strategy}: 直近7日間でエントリーなし",
                    value=0.0,
                    threshold=7.0,  # 7日
                    severity="LOW"
                ))

        return alerts

    def generate_report(self) -> str:
        """Markdown形式でアラート状況レポートを返す"""
        alerts = self.check_strategies()

        lines = []
        lines.append("# 戦略別アラートレポート")
        lines.append(f"生成時刻: {datetime.now().isoformat()}")
        lines.append("")

        if not alerts:
            lines.append("✅ アラートはありません")
            return "\n".join(lines)

        # Severityごとにグループ化
        alerts_by_severity = {}
        for alert in alerts:
            severity = alert.severity
            if severity not in alerts_by_severity:
                alerts_by_severity[severity] = []
            alerts_by_severity[severity].append(alert)

        # HIGH, MEDIUM, LOWの順に出力
        for severity in ["HIGH", "MEDIUM", "LOW"]:
            if severity not in alerts_by_severity:
                continue

            severity_label = {
                "HIGH": "🔴 **危険**",
                "MEDIUM": "🟡 **注意**",
                "LOW": "🔵 **情報**"
            }[severity]

            lines.append(f"## {severity_label}")
            lines.append("")

            for alert in alerts_by_severity[severity]:
                lines.append(f"### {alert.strategy} ({alert.alert_type})")
                lines.append(f"- {alert.message}")
                lines.append(f"- 実際の値: {alert.value:.2f}")
                lines.append(f"- 閾値: {alert.threshold:.2f}")
                lines.append("")

        lines.append("---")
        lines.append(f"総アラート数: {len(alerts)}")

        return "\n".join(lines)

    def send_discord_alert(self) -> bool:
        """Discord に通知（DISCORD_WEBHOOK_URL 環境変数がある場合のみ）"""
        webhook_url = os.environ.get('DISCORD_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_YORIAI')
        if not webhook_url:
            return False

        try:
            import requests
        except ImportError:
            return False

        alerts = self.check_strategies()
        if not alerts:
            return True  # アラートなし = 通知成功

        # Embed形式で整形
        embed = {
            "title": "戦略別アラート通知",
            "description": f"総{len(alerts)}件のアラート",
            "color": 16711680,  # 赤
            "timestamp": datetime.now().isoformat(),
            "fields": []
        }

        for alert in alerts:
            color_map = {
                "HIGH": 16711680,    # 赤
                "MEDIUM": 16776960,  # 黄
                "LOW": 255           # 青
            }
            embed["color"] = color_map.get(alert.severity, 255)

            embed["fields"].append({
                "name": f"{alert.strategy} ({alert.alert_type})",
                "value": alert.message,
                "inline": False
            })

        payload = {"embeds": [embed]}

        try:
            response = requests.post(
                webhook_url,
                json=payload,
                timeout=5
            )
            return response.status_code == 204
        except Exception:
            return False


def main():
    """CLI エントリーポイント"""
    parser = ArgumentParser(description='戦略別アラートシステム')
    parser.add_argument(
        '--report',
        action='store_true',
        help='Markdown形式で詳細レポートを出力'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='JSON形式でアラートを出力'
    )
    parser.add_argument(
        '--portfolio',
        type=str,
        default='paper_portfolio.json',
        help='ポートフォリオファイルパス'
    )
    parser.add_argument(
        '--send-discord',
        action='store_true',
        help='Discord に通知を送信'
    )

    args = parser.parse_args()

    try:
        system = StrategyAlertSystem(portfolio_path=args.portfolio)
        system.load_data()

        if args.json:
            alerts = system.check_strategies()
            output = json.dumps(
                [asdict(a) for a in alerts],
                ensure_ascii=False,
                indent=2
            )
            print(output)
        elif args.report:
            print(system.generate_report())
        elif args.send_discord:
            success = system.send_discord_alert()
            status = "成功" if success else "失敗 (要因: 環境変数未設定 or requests未インストール)"
            print(f"Discord通知: {status}")
        else:
            alerts = system.check_strategies()
            if not alerts:
                print("✅ アラートはありません")
            else:
                print(f"⚠️  {len(alerts)}件のアラートを検出:")
                for alert in alerts:
                    severity_icon = {
                        "HIGH": "🔴",
                        "MEDIUM": "🟡",
                        "LOW": "🔵"
                    }[alert.severity]
                    print(f"{severity_icon} [{alert.alert_type}] {alert.message}")

    except FileNotFoundError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"予期しないエラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

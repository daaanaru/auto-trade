"""
notifier.py — 統合通知モジュール

シグナル発生・トレード実行・卒業条件クリアを外部サービスに通知する。

対応チャネル:
  - Discord Webhook（即利用可能）
  - Google Calendar（google-api-python-client 要セットアップ）

.envに以下を設定:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  GOOGLE_CALENDAR_CREDENTIALS=path/to/credentials.json  (オプション)
"""

import json
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# DISCORD_WEBHOOK_URL を優先し、未設定なら DISCORD_WEBHOOK_YORIAI にフォールバック
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_YORIAI", "")
GOOGLE_CALENDAR_CREDENTIALS = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS", "")


# ==============================================================
# Discord
# ==============================================================

def send_discord(message: str, username: str = "auto-trade"):
    """Discord Webhookにメッセージを送信する。"""
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        payload = {
            "content": message,
            "username": username,
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"  [NOTIFY] Discord送信失敗: {e}")
        return False


def send_discord_embed(title: str, description: str, color: int = 0x00FF00,
                       fields: list = None, username: str = "auto-trade"):
    """Discord Webhookにリッチなembedメッセージを送信する。"""
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if fields:
            embed["fields"] = fields

        payload = {
            "username": username,
            "embeds": [embed],
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"  [NOTIFY] Discord Embed送信失敗: {e}")
        return False


# ==============================================================
# 通知ヘルパー（用途別）
# ==============================================================

def notify_buy_signal(code: str, name: str, market: str, price: float,
                      strategy: str, score: float, fundamental: dict = None,
                      reason: str = ""):
    """BUYシグナル検出時の通知。"""
    # Discord
    fields = [
        {"name": "銘柄", "value": f"{name} ({code})", "inline": True},
        {"name": "市場", "value": market.upper(), "inline": True},
        {"name": "価格", "value": f"{price:,.2f}", "inline": True},
        {"name": "戦略", "value": strategy, "inline": True},
        {"name": "スコア", "value": f"{score:.1f}", "inline": True},
    ]
    if reason:
        fields.append({
            "name": "シグナル理由",
            "value": reason,
            "inline": False,
        })
    if fundamental:
        fields.append({
            "name": "ファンダメンタル",
            "value": f"スコア: {fundamental.get('score', 0):+.1f} / {fundamental.get('reason', 'N/A')}",
            "inline": False,
        })

    send_discord_embed(
        title=f"BUY {name} ({code})",
        description=f"{market.upper()}市場でBUYシグナル検出",
        color=0x00FF00,  # 緑
        fields=fields,
    )

    # Google Calendar
    _add_calendar_event(
        summary=f"[BUY] {name}({code}) @ {price:,.0f}",
        description=f"戦略: {strategy}\nスコア: {score:.1f}\n"
                    f"ファンダ: {fundamental.get('reason', 'N/A') if fundamental else 'N/A'}",
    )


def notify_sell_signal(code: str, name: str, price: float, pnl_jpy: float,
                       pnl_pct: float, reason: str):
    """売りシグナル/損切り/利確の通知。"""
    color = 0xFF0000 if pnl_jpy < 0 else 0x00FF00
    emoji = "🔻" if pnl_jpy < 0 else "🔺"

    send_discord_embed(
        title=f"SELL {name} ({code}) {emoji}",
        description=f"理由: {reason}",
        color=color,
        fields=[
            {"name": "価格", "value": f"{price:,.2f}", "inline": True},
            {"name": "損益", "value": f"{pnl_jpy:+,.0f} JPY ({pnl_pct:+.1f}%)", "inline": True},
        ],
    )


def notify_portfolio_update(total_value: float, total_return_pct: float,
                            position_count: int, cash: float):
    """ポートフォリオ日次更新通知。"""
    emoji = "📈" if total_return_pct >= 0 else "📉"
    deadline = datetime(2026, 4, 12)
    remaining = (deadline - datetime.now()).days

    send_discord_embed(
        title=f"{emoji} 日次ポートフォリオレポート",
        description=f"残り{remaining}日（期限: 4/12）",
        color=0x3498DB,  # 青
        fields=[
            {"name": "総資産", "value": f"{total_value:,.0f} JPY", "inline": True},
            {"name": "収益率", "value": f"{total_return_pct:+.1f}%", "inline": True},
            {"name": "ポジション", "value": f"{position_count}/50", "inline": True},
            {"name": "現金", "value": f"{cash:,.0f} JPY", "inline": True},
        ],
    )


def notify_graduation(passed: bool, checks: list, summary: dict):
    """卒業判定結果の通知。"""
    if passed:
        title = "GRADUATED — 卒業条件クリア！"
        color = 0xFFD700  # ゴールド
        desc = "全条件をクリアしました。DRY_RUNテストに進めます。"
    else:
        passed_count = sum(1 for c in checks if c.get("passed"))
        title = f"卒業判定: {passed_count}/{len(checks)} 条件達成"
        color = 0x95A5A6  # グレー
        failed = [c["name"] for c in checks if not c.get("passed")]
        desc = f"未達項目: {', '.join(failed)}"

    fields = []
    for c in checks:
        mark = "PASS" if c.get("passed") else "FAIL"
        fields.append({
            "name": f"[{mark}] {c['name']}",
            "value": f"基準: {c['required']} / 実績: {c['actual']}",
            "inline": False,
        })

    send_discord_embed(title=title, description=desc, color=color, fields=fields)


def notify_scan_summary(markets_data: list):
    """全市場スキャン結果のサマリー通知（理由付き）。"""
    total_buy = sum(d["summary"]["buy"] for d in markets_data)
    if total_buy == 0:
        return  # BUYシグナルがなければ通知しない

    # 戦略名の日本語マップ
    strategy_labels = {
        "monthly": "月初モメンタム",
        "bb_rsi": "BB+RSI逆張り",
        "vol_div": "出来高ダイバージェンス",
    }

    fields = []
    for d in markets_data:
        strategy_key = d.get("strategy", "")
        strategy_ja = strategy_labels.get(strategy_key, strategy_key)
        for r in d["results"]:
            if r["signal"] == "BUY" and len(fields) < 10:
                reason = r.get("reason", "")
                value_lines = [f"価格: {r['price']:,.2f} / スコア: {r['score']:.1f}"]
                if reason:
                    value_lines.append(f"理由: {reason}")
                value_lines.append(f"戦略: {strategy_ja}")
                fields.append({
                    "name": f"{r['name']}({r['code']})",
                    "value": "\n".join(value_lines),
                    "inline": False,
                })

    send_discord_embed(
        title=f"BUYシグナル {total_buy}件検出",
        description="以下の銘柄でBUYシグナルが発生しました",
        color=0x2ECC71,
        fields=fields,
    )


# ==============================================================
# Google Calendar
# ==============================================================

def _add_calendar_event(summary: str, description: str = "",
                        minutes_from_now: int = 60):
    """Google Calendarに予定を追加する。

    google-api-python-client + OAuth2 認証が必要。
    認証情報がなければスキップする。
    """
    if not GOOGLE_CALENDAR_CREDENTIALS:
        return False

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds_path = GOOGLE_CALENDAR_CREDENTIALS
        if not os.path.exists(creds_path):
            return False

        creds = Credentials.from_authorized_user_file(creds_path)
        service = build("calendar", "v3", credentials=creds)

        now = datetime.now()
        start = now + timedelta(minutes=minutes_from_now)
        end = start + timedelta(minutes=30)

        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"},
            "reminders": {"useDefault": True},
        }

        service.events().insert(calendarId="primary", body=event).execute()
        print(f"  [NOTIFY] Google Calendar: {summary}")
        return True

    except ImportError:
        return False
    except Exception as e:
        print(f"  [NOTIFY] Google Calendar失敗: {e}")
        return False

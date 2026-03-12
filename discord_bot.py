#!/usr/bin/env python3
"""
discord_bot.py — DaNARU幕府 Discord Bot

Discordのメッセージを受け取り、以下のコマンドに応答する:
- 「予定」「スケジュール」→ Googleカレンダーに予定登録（claude -p経由）
- 「ダッシュボード」「損益」→ auto-tradeの状況を返す
- 「taisho」「マイクラ」→ taishoの死亡数・最新ログ
- 「ステータス」「状態」→ 全システムの稼働状況
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime

from typing import Optional

import discord

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 設定
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANY_DIR = os.path.dirname(os.path.dirname(BASE_DIR))
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# .envから読み込み
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", BOT_TOKEN)

if not BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN が未設定です。.env に追加してください。")
    sys.exit(1)

# Discord Client設定
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# launchd環境用のPATH（nodeが見つからない問題の対策）
CLAUDE_ENV = {
    "HOME": os.path.expanduser("~"),
    "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "TERM": "xterm-256color",
}


def run_command(cmd: str, timeout: int = 30) -> str:
    """シェルコマンドを実行して出力を返す。

    セキュリティ注意（火付盗賊改 2026-03-12）:
    shell=True を使用している。呼び出し元は全てハードコード文字列のみ。
    ユーザー入力や外部データを cmd に渡すことは絶対に禁止。
    パイプ（|）を多用するためshell=Trueを維持するが、
    新規コマンド追加時はインジェクションリスクを必ず検討すること。
    """
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() or result.stderr.strip() or "(出力なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト)"
    except Exception as e:
        return f"(エラー: {e})"


def run_claude(prompt: str, timeout: int = 60) -> str:
    """claude -p でプロンプトを実行して結果を返す。"""
    # CLAUDECODE除外 + launchd環境用のPATH補完
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env.update(CLAUDE_ENV)
    try:
        logger.info("claude -p 実行開始: prompt='%s'", prompt[:80])
        result = subprocess.run(
            ["/opt/homebrew/bin/claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=BASE_DIR,
        )
        output = result.stdout.strip()
        if result.stderr.strip():
            logger.warning("claude stderr: %s", result.stderr.strip()[:200])
        if result.returncode != 0:
            logger.warning("claude returncode: %d", result.returncode)
        if len(output) > 1900:
            output = output[:1900] + "\n...(省略)"
        return output or "(応答なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト: 60秒超過)"
    except FileNotFoundError:
        return "(claude コマンドが見つかりません)"
    except Exception as e:
        logger.error("claude -p 例外: %s", e)
        return f"(エラー: {e})"


def get_dashboard() -> str:
    """auto-tradeダッシュボードの要約を返す。"""
    portfolio_path = os.path.join(BASE_DIR, "paper_portfolio.json")
    if not os.path.exists(portfolio_path):
        return "ポートフォリオファイルが見つかりません。"

    try:
        with open(portfolio_path) as f:
            p = json.load(f)

        cash = p.get("cash_jpy", 0)
        positions = p.get("positions", [])
        initial = p.get("initial_capital_jpy", 30000)
        leverage = p.get("leverage", 1)

        lines = [
            f"**$200ペーパートレード**",
            f"初期資金: ¥{initial:,.0f} / レバレッジ: {leverage}倍",
            f"現金: ¥{cash:,.0f}",
            f"ポジション数: {len(positions)}/{5}",
            "",
        ]
        for pos in positions:
            side = pos.get("side", "long")
            lines.append(
                f"  {pos.get('name', pos['code'])} ({side}) "
                f"entry=${pos['entry_price']:.1f}"
            )

        closed = p.get("closed_trades", [])
        realized = p.get("total_realized_pnl", 0)
        lines.append(f"\n決済済み: {len(closed)}件 / 実現損益: ¥{realized:,.0f}")

        return "\n".join(lines)
    except Exception as e:
        return f"ダッシュボード取得エラー: {e}"


def get_taisho_status() -> str:
    """taishoの最新状況を返す。"""
    lines = []

    # 死亡数カウント
    death_count = run_command(
        '(for f in ~/minecraft-server-v2/logs/*.log.gz; do gzcat "$f" 2>/dev/null; done; '
        'cat ~/minecraft-server-v2/logs/latest.log 2>/dev/null) | '
        'grep -c "taisho was\\|taisho drowned\\|taisho blew up\\|taisho fell"',
        timeout=15,
    )
    lines.append(f"**taisho 死亡回数**: {death_count}回")

    # プロセス確認
    ps_check = run_command('ps aux | grep "node main.js" | grep -v grep | wc -l')
    alive = ps_check.strip() != "0"
    lines.append(f"**MindCraft**: {'稼働中' if alive else '停止中'}")

    # 最新ログ
    latest = run_command(
        'grep "<taisho>" ~/minecraft-server-v2/logs/latest.log | '
        'grep -v " \\!" | grep -v "ああ！" | tail -3'
    )
    if latest and latest != "(出力なし)":
        lines.append(f"\n**最新セリフ**:\n```\n{latest}\n```")

    return "\n".join(lines)


def get_system_status() -> str:
    """全システムの稼働状況を返す。"""
    jobs = run_command("launchctl list | grep danaru | wc -l")
    uptime = run_command("uptime")

    # 常駐プロセス確認
    line_agent = run_command('ps aux | grep "line-agent" | grep -v grep | wc -l')
    ngrok = run_command('ps aux | grep "ngrok" | grep -v grep | wc -l')
    paper = run_command('ps aux | grep "paper.jar" | grep -v grep | wc -l')
    mindcraft = run_command('ps aux | grep "node main.js" | grep -v grep | wc -l')

    lines = [
        f"**launchdジョブ**: {jobs.strip()}個登録",
        f"**uptime**: {uptime}",
        "",
        "**常駐プロセス**:",
        f"  LINE Bot: {'稼働' if line_agent.strip() != '0' else '停止'}",
        f"  ngrok: {'稼働' if ngrok.strip() != '0' else '停止'}",
        f"  Paper MC: {'稼働' if paper.strip() != '0' else '停止'}",
        f"  MindCraft: {'稼働' if mindcraft.strip() != '0' else '停止'}",
    ]
    return "\n".join(lines)


def parse_schedule_request(content: str) -> Optional[dict]:
    """メッセージからスケジュール情報を抽出する。"""
    # 「明日14時にミーティング」「3/15 10:00 打ち合わせ」のようなパターン
    # claude -p に投げて解析させるのが一番確実
    return {"raw": content}


@client.event
async def on_ready():
    logger.info("Discord Bot ログイン完了: %s (ID: %s)", client.user, client.user.id)


@client.event
async def on_message(message: discord.Message):
    logger.info("メッセージ受信: author=%s content='%s' guild=%s channel=%s",
                message.author, message.content[:50], message.guild, message.channel)

    # 自分のメッセージには反応しない
    if message.author == client.user:
        return

    # Bot宛てのメッセージかチャンネル内の特定キーワード
    content = message.content.strip()
    if not content:
        return

    # メンション or 特定キーワードで反応
    is_mentioned = client.user in message.mentions
    lower = content.lower()

    # --- 予定・スケジュール登録 ---
    if is_mentioned and ("予定" in content or "スケジュール" in content or "カレンダー" in content):
        await message.channel.send("予定を登録します...")
        # メンション部分を除去
        clean = re.sub(r"<@!?\d+>", "", content).strip()
        prompt = (
            f"以下の内容をGoogleカレンダーに予定として登録してください。"
            f"日時が曖昧な場合は確認せず、最も妥当な解釈で登録してください。"
            f"今日は{datetime.now().strftime('%Y年%m月%d日')}です。\n\n"
            f"「{clean}」\n\n"
            f"登録したら、タイトル・日時・場所を簡潔に報告してください。"
        )
        result = await asyncio.to_thread(run_claude, prompt, 90)
        await message.channel.send(result)
        return

    # --- ダッシュボード ---
    if is_mentioned and ("ダッシュボード" in content or "損益" in content or "トレード" in content or "ポートフォリオ" in content):
        result = get_dashboard()
        await message.channel.send(result)
        return

    # --- taisho / マイクラ ---
    if is_mentioned and ("taisho" in lower or "マイクラ" in content or "大匠" in content):
        result = get_taisho_status()
        await message.channel.send(result)
        return

    # --- ステータス ---
    if is_mentioned and ("ステータス" in content or "状態" in content or "稼働" in content):
        result = get_system_status()
        await message.channel.send(result)
        return

    # --- 汎用（メンション時） ---
    if is_mentioned:
        clean = re.sub(r"<@!?\d+>", "", content).strip()
        if clean:
            await message.channel.send("考え中...")
            result = await asyncio.to_thread(run_claude, clean, 90)
            await message.channel.send(result)
        return


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("DISCORD_BOT_TOKEN を .env に設定してください。")
        sys.exit(1)
    client.run(BOT_TOKEN, log_handler=None)

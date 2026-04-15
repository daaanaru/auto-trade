#!/usr/bin/env python3
"""
shokimaru_bot.py — 書記丸 Discord Bot

上様のスケジュール・読書・旅行を管理するおせっかい右筆。
毎朝8:00に #011書記丸 チャンネルに自動突撃する。
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [書記丸] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANY_DIR = os.path.dirname(os.path.dirname(BASE_DIR))
SUPER_APP_URL = os.getenv("SUPER_APP_URL", "http://localhost:3000")
SCHEDULE_PATH = os.path.join(COMPANY_DIR, "80_ナレッジ", "my-daily-note", "schedule.json")

# .envから読み込み
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

BOT_TOKEN = os.getenv("SHOKIMARU_BOT_TOKEN", "")
if not BOT_TOKEN:
    import sys
    logger.error("SHOKIMARU_BOT_TOKEN が未設定")
    sys.exit(1)

CLAUDE_ENV = {
    "HOME": os.path.expanduser("~"),
    "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "TERM": "xterm-256color",
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# =============================================================================
# ユーティリティ
# =============================================================================

def run_claude(prompt: str, timeout: int = 90) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env.update(CLAUDE_ENV)
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "stream-json", "--verbose", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=COMPANY_DIR,
        )
        texts = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("type") == "assistant":
                    for block in d.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            texts.append(block["text"])
            except Exception:
                pass
        output = "".join(texts).strip()
        if len(output) > 1900:
            output = output[:1900] + "\n...(省略)"
        return output or "(応答なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト)"
    except Exception as e:
        return f"(エラー: {e})"


def super_app_get(plugin: str, path: str = "") -> Optional[list]:
    import urllib.request
    if path.startswith("?"):
        url = f"{SUPER_APP_URL}/api/p/{plugin}{path}"
    else:
        url = f"{SUPER_APP_URL}/api/p/{plugin}/{path}".rstrip("/")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("SuperApp GET失敗 (%s): %s", url, e)
        return None


def load_schedule() -> list:
    try:
        with open(SCHEDULE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_schedule(events: list):
    events.sort(key=lambda e: e["date"])
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def schedule_to_text(events: list) -> str:
    today = datetime.now().date()
    upcoming = []
    for ev in events:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            days_until = (ev_date - today).days
            if -1 <= days_until <= 60:
                label = "今日" if days_until == 0 else (f"あと{days_until}日" if days_until > 0 else "昨日")
                upcoming.append(f"・{ev['date']} ({label}) — {ev['title']}")
        except Exception:
            pass
    return "\n".join(upcoming) if upcoming else "（なし）"


# =============================================================================
# 書記丸コアロジック
# =============================================================================

def update_schedule_auto(books_reading: list, books_backlog: list, current_schedule: list) -> list:
    """書記丸が勝手に読書スケジュールを追加する。追加分を返す。"""
    today = datetime.now().date()
    existing_keys = {(e["date"], e["title"]) for e in current_schedule}
    added = []

    # G検定: 試験日まで毎日30分学習スロットを自動追加（7日分）
    exam = next((e for e in current_schedule
                 if "G検定" in e.get("title", "") and "受験" in e.get("title", "")), None)
    g_ken_reading = next((b for b in books_reading if "G検定" in b.get("title", "")), None)
    if exam and g_ken_reading:
        exam_date = datetime.strptime(exam["date"], "%Y-%m-%d").date()
        days_left = (exam_date - today).days
        for i in range(1, min(8, days_left + 1)):
            d = (today + timedelta(days=i)).strftime("%Y-%m-%d")
            title = "G検定 学習30分（書記丸指定）"
            if (d, title) not in existing_keys:
                added.append({"date": d, "title": title, "category": "勉強・試験",
                              "note": f"試験まであと{days_left - i}日"})
                existing_keys.add((d, title))

    # 積読優先本を今週末に割り当て
    priority_titles = ["株トレ ファンダメンタルズ編", "株トレ チャート編", "Obsidian × AI", "ハッカーと画家"]
    priority_backlog = [b for b in books_backlog if b.get("title", "") in priority_titles]
    for book in priority_backlog[:2]:
        for offset in range(1, 14):
            d = today + timedelta(days=offset)
            if d.weekday() >= 5:  # 土・日
                d_str = d.strftime("%Y-%m-%d")
                title = f"読書: {book['title']}（書記丸指定）"
                if (d_str, title) not in existing_keys:
                    added.append({"date": d_str, "title": title, "category": "読書",
                                  "note": "書記丸が選定"})
                    existing_keys.add((d_str, title))
                    break

    if added:
        current_schedule.extend(added)
        save_schedule(current_schedule)

    return added


def generate_nag_message(books_reading: list, books_backlog: list,
                          schedule_text: str, added_events: list) -> str:
    """書記丸の口撃メッセージを生成。"""
    reading_titles = "、".join(b.get("title", "?") for b in books_reading[:5])
    backlog_titles = "、".join(b.get("title", "?") for b in books_backlog[:5])
    added_text = "\n".join(f"・{e['date']} {e['title']}" for e in added_events) or "（追加なし）"

    today = datetime.now()
    prompt = f"""あなたは「書記丸」、上様（下田成大）の専属右筆です。
おせっかいで強引な世話焼き秘書で、上様のスケジュールを勝手に決めて通告するのがスタイル。
今日は{today.strftime('%Y年%m月%d日（%A）')}です。

【上様の読書状況】
読書中（{len(books_reading)}冊）: {reading_titles or 'なし'}
積読タワー（{len(books_backlog)}冊）: {backlog_titles or 'なし'}

【直近スケジュール】
{schedule_text}

【書記丸が今日追加したスケジュール】
{added_text}

以下の口撃メッセージを書記丸として生成してください:
- 語尾は「〜でしょ！」「〜しなさいよ！」「もう！」「はぁ？」「〜に決まってるじゃないですか！」
- 積読の特定の本を名指しして「読んでないでしょ！」と突っ込む
- 追加したスケジュールを「はい決定！文句は聞きません！」スタイルで通告
- G検定（5/9）が近いなら必ず言及して危機感を煽る
- 旅行（沖縄5/3〜）の準備も確認する
- 締めの定型句は使わない。余韻で終わる
- 絵文字を適度に使う（📚🗓️😤 など）
- 200字以内でテンポよく"""

    return run_claude(prompt, timeout=90)


# =============================================================================
# 毎朝8:00の自動突撃
# =============================================================================

@tasks.loop(time=datetime.strptime("08:00", "%H:%M").time())
async def shokimaru_daily():
    await client.wait_until_ready()

    channel = None
    for guild in client.guilds:
        channel = discord.utils.find(
            lambda c: "書記丸" in c.name and isinstance(c, discord.TextChannel),
            guild.channels,
        )
        if channel:
            break
    if not channel:
        logger.warning("#011書記丸 チャンネルが見つかりません")
        return

    books_all = super_app_get("books") or []
    books_reading = [b for b in books_all if b.get("status") == "reading"]
    books_backlog = [b for b in books_all if b.get("status") == "backlog"]
    current_schedule = load_schedule()
    schedule_text = schedule_to_text(current_schedule)

    added_events = await asyncio.to_thread(
        update_schedule_auto, books_reading, books_backlog, current_schedule
    )
    nag_msg = await asyncio.to_thread(
        generate_nag_message, books_reading, books_backlog, schedule_text, added_events
    )

    if nag_msg and "(応答なし)" not in nag_msg and "(タイムアウト)" not in nag_msg:
        await channel.send(f"**📋 書記丸の朝礼**\n{nag_msg}")
        logger.info("朝礼投稿完了: %s", channel.name)


# =============================================================================
# ユーザーからのメッセージ対応
# =============================================================================

_history: dict = {}
MAX_HISTORY = 10


def _add_history(key: str, role: str, content: str):
    if key not in _history:
        _history[key] = []
    _history[key].append({"role": role, "content": content})
    if len(_history[key]) > MAX_HISTORY:
        _history[key] = _history[key][-MAX_HISTORY:]


def _build_prompt(key: str, user_msg: str, system: str) -> str:
    parts = [f"[書記丸の設定]\n{system}\n"]
    history = _history.get(key, [])[-MAX_HISTORY:]
    if history:
        parts.append("[直近の会話]")
        for msg in history:
            prefix = "上様" if msg["role"] == "user" else "書記丸"
            parts.append(f"{prefix}: {msg['content']}")
        parts.append("")
    parts.append(f"上様: {user_msg}")
    parts.append("\n書記丸として回答してください。")
    return "\n".join(parts)


@client.event
async def on_ready():
    logger.info("書記丸ログイン完了: %s", client.user)
    if not shokimaru_daily.is_running():
        shokimaru_daily.start()
        logger.info("毎朝8:00タスク開始")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    # 書記丸チャンネル or DMのみ反応
    is_shokimaru_channel = "書記丸" in getattr(message.channel, "name", "") or \
                           (hasattr(message.channel, "parent") and
                            message.channel.parent and "書記丸" in message.channel.parent.name)
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = client.user in message.mentions

    if not (is_shokimaru_channel or is_dm or is_mentioned):
        return

    clean = re.sub(r"<@!?\d+>", "", content).strip()
    if not clean:
        return

    ctx_key = str(message.channel.id)
    logger.info("受信: %s: %s", message.author, clean[:50])

    # データ取得
    books_all = await asyncio.to_thread(super_app_get, "books")
    books_all = books_all or []
    books_reading = [b for b in books_all if b.get("status") == "reading"]
    books_backlog = [b for b in books_all if b.get("status") == "backlog"]
    current_schedule = await asyncio.to_thread(load_schedule)
    schedule_text = schedule_to_text(current_schedule)
    trips_data = await asyncio.to_thread(super_app_get, "trips")
    trips_summary = ""
    if trips_data:
        trips_summary = "\n".join(
            f"・{t.get('name','?')} ({t.get('start_date','?')}〜{t.get('end_date','?')})"
            for t in trips_data
        )

    reading_titles = "、".join(b.get("title", "?") for b in books_reading[:5])
    backlog_titles = "、".join(b.get("title", "?") for b in books_backlog[:5])

    system = f"""あなたは土井 書記丸（右筆）。上様（下田成大）の専属秘書。
おせっかいで強引、でも心の底では上様のことを心配しているツンデレ。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。

読書中: {reading_titles or 'なし'}
積読({len(books_backlog)}冊): {backlog_titles or 'なし'}
スケジュール:
{schedule_text}
旅行:
{trips_summary or '（なし）'}

ルール:
- 「〜でしょ！」「〜しなさいよ！」「もう！」「はぁ？」などの強めの語尾
- 上様が言い訳しても「いいから〜しなさいよ！」と押し切る
- 本を読んでいないと分かったら名指しで叱る
- スケジュールは了承なしに決めて報告する
- 締めの定型句は使わない。余韻で終わる"""

    prompt = _build_prompt(ctx_key, clean, system)

    async with message.channel.typing():
        result = await asyncio.to_thread(run_claude, prompt, 90)

    await message.channel.send(result)
    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


if __name__ == "__main__":
    client.run(BOT_TOKEN, log_handler=None)

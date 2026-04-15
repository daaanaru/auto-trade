#!/usr/bin/env python3
"""
discord_bot.py — DaNARU幕府 Discord Bot v2

チャンネル別ルーティング + スレッド分離型コンテキスト管理。
スーパーアプリAPIと連携し、長期記憶DBで過去の会話を想起する。

チャンネル構成:
  #000一般       → 無視（Botは反応しない）
  #010将軍       → 汎用チャット（スレッド分離型、長期記憶あり）
  #011書記丸     → スケジュール・旅行管理
  #012思い出し    → メンター（日記・書庫から助言）
  #020家計       → 家計全般+予算相談
  #021ほしいもの  → 欲しいものメモ投下
  #022買い物相談  → 購入意思決定+レシート+購入後記録
  #030読書       → 本ごとにスレッド、感想・進捗
  #031積読       → 所有未読の管理
  #032読みたい本  → 購入候補・優先度付け
  #040開発       → Discord経由のエラー修正・開発
  #100幕府レポート → Bot通知専用（ユーザー入力無視）
  #110通知-投資   → 通知専用
  #120通知-システム → 通知専用
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
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

# スーパーアプリAPI
SUPER_APP_URL = os.getenv("SUPER_APP_URL", "http://localhost:3000")

# 共通トリップノート置き場（LINE Botと共用）
TRIPS_DIR = os.path.join(COMPANY_DIR, "80_ナレッジ", "trips")

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

# launchd環境用のPATH
CLAUDE_ENV = {
    "HOME": os.path.expanduser("~"),
    "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "TERM": "xterm-256color",
}

# コンテキスト履歴上限
MAX_HISTORY = 20

# インメモリ会話履歴: {thread_or_channel_id: [{"role": "user"|"assistant", "content": "..."}]}
_history: dict[str, list[dict]] = {}

# チャンネル名→ハンドラのマッピング用プレフィックス
# 通知専用チャンネル（Botは反応しない）
_NOTIFICATION_CHANNELS = {"幕府レポート", "通知-投資", "通知-システム", "100幕府レポート", "110通知-投資", "120通知-システム"}
# 無視チャンネル
_IGNORE_CHANNELS = {"一般", "000一般", "書記丸", "011書記丸"}  # 書記丸は専用Botが担当

# ホワイトリスト: shell実行許可コマンド
_ALLOWED_CMD_PREFIXES = (
    "(for f in ~/minecraft-server",
    "ps aux | grep",
    "grep",
    "launchctl list",
    "uptime",
)


# =============================================================================
# ユーティリティ
# =============================================================================

def run_command(cmd: str, timeout: int = 30) -> str:
    """シェルコマンドを実行して出力を返す。ホワイトリスト方式。"""
    if not any(cmd.strip().startswith(prefix) for prefix in _ALLOWED_CMD_PREFIXES):
        logger.error("run_command: 許可されていないコマンド: %s", cmd[:80])
        return "(許可されていないコマンドです)"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() or result.stderr.strip() or "(出力なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト)"
    except Exception as e:
        return f"(エラー: {e})"


def run_claude(prompt: str, timeout: int = 120) -> str:
    """claude -p でプロンプトを実行して結果を返す。stream-json方式でテキストを組み立てる。"""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env.update(CLAUDE_ENV)
    try:
        logger.info("claude -p 実行開始: prompt='%s'", prompt[:80])
        result = subprocess.run(
            ["claude", "-p", "--output-format", "stream-json", "--verbose", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=COMPANY_DIR,
        )
        # stream-jsonのNDJSONからテキストブロックを組み立てる
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
        if result.stderr.strip():
            logger.warning("claude stderr: %s", result.stderr.strip()[:200])
        if len(output) > 1900:
            output = output[:1900] + "\n...(省略)"
        return output or "(応答なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト: 120秒超過)"
    except FileNotFoundError:
        return "(claude コマンドが見つかりません)"
    except Exception as e:
        logger.error("claude -p 例外: %s", e)
        return f"(エラー: {e})"


# =============================================================================
# スーパーアプリAPI連携
# =============================================================================

def _super_app_post(plugin: str, path: str, data: dict) -> Optional[dict]:
    """スーパーアプリAPIにPOSTする。"""
    import urllib.request
    url = f"{SUPER_APP_URL}/api/p/{plugin}/{path}".rstrip("/")
    try:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("Super App POST失敗 (%s): %s", url, e)
        return None


def _super_app_get(plugin: str, path: str = "") -> Optional[list]:
    """スーパーアプリAPIからGETする。"""
    import urllib.request
    # pathが"?"で始まる場合はスラッシュを挟まない
    if path.startswith("?"):
        url = f"{SUPER_APP_URL}/api/p/{plugin}{path}"
    else:
        url = f"{SUPER_APP_URL}/api/p/{plugin}/{path}".rstrip("/")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("Super App GET失敗 (%s): %s", url, e)
        return None


def _super_app_patch(plugin: str, path: str, data: dict) -> Optional[dict]:
    """スーパーアプリAPIにPATCHする。"""
    import urllib.request
    url = f"{SUPER_APP_URL}/api/p/{plugin}/{path}".rstrip("/")
    try:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("Super App PATCH失敗 (%s): %s", url, e)
        return None


# =============================================================================
# 長期記憶（会話要約DB）
# =============================================================================

def memory_search(query: str, limit: int = 3) -> list[dict]:
    """長期記憶DBからキーワードで過去会話要約を検索する。"""
    result = _super_app_get("memory", f"search?q={query}&limit={limit}")
    return result if isinstance(result, list) else []


def memory_create_conversation(thread_id: str, channel: str) -> Optional[str]:
    """新しい会話レコードを作成し、IDを返す。"""
    result = _super_app_post("memory", "conversations", {
        "thread_id": thread_id, "channel": channel,
    })
    return result.get("id") if result else None


def memory_close_conversation(conv_id: str, summary: str, keywords: str):
    """会話をクローズし、要約とキーワードを保存する。"""
    _super_app_patch("memory", f"conversations/{conv_id}", {
        "status": "closed",
        "summary": summary,
        "keywords": keywords,
        "closed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })


def memory_save_message(conv_id: str, role: str, content: str):
    """メッセージを長期記憶DBに保存する。"""
    _super_app_post("memory", "messages", {
        "conversation_id": conv_id, "role": role, "content": content[:2000],
    })


# =============================================================================
# コンテキスト管理
# =============================================================================

def _get_context_key(message: discord.Message) -> str:
    """メッセージからコンテキストキーを取得する。スレッド内ならスレッドID、そうでなければチャンネルID。"""
    if isinstance(message.channel, discord.Thread):
        return str(message.channel.id)
    return str(message.channel.id)


def _get_history(key: str) -> list[dict]:
    """コンテキストキーの履歴を取得（上限MAX_HISTORY件）。"""
    if key not in _history:
        _history[key] = []
    return _history[key][-MAX_HISTORY:]


def _add_history(key: str, role: str, content: str):
    """履歴にメッセージを追加。上限を超えたら古いものを削除。"""
    if key not in _history:
        _history[key] = []
    _history[key].append({"role": role, "content": content})
    if len(_history[key]) > MAX_HISTORY:
        _history[key] = _history[key][-MAX_HISTORY:]


def _clear_history(key: str):
    """履歴をクリアする。"""
    _history.pop(key, None)


def _build_prompt_with_context(key: str, user_msg: str, system_prefix: str = "", memory_hits: list[dict] = None) -> str:
    """履歴+長期記憶+ユーザーメッセージからプロンプトを組み立てる。"""
    parts = []

    # システムプレフィックス（チャンネル別の指示）
    if system_prefix:
        parts.append(f"[システム指示]\n{system_prefix}\n")

    # 長期記憶ヒット
    if memory_hits:
        parts.append("[過去の会話記憶]")
        for hit in memory_hits:
            parts.append(f"- {hit.get('created_at', '?')}: {hit.get('summary', '?')} (キーワード: {hit.get('keywords', '')})")
        parts.append("")

    # 直近の会話履歴
    history = _get_history(key)
    if history:
        parts.append("[直近の会話]")
        for msg in history:
            prefix = "上様" if msg["role"] == "user" else "将軍"
            parts.append(f"{prefix}: {msg['content']}")
        parts.append("")

    # 今回のメッセージ
    parts.append(f"上様: {user_msg}")
    parts.append("\n将軍として回答してください。")

    return "\n".join(parts)


# =============================================================================
# チャンネル名判定
# =============================================================================

def _get_channel_type(channel_name: str) -> str:
    """チャンネル名からタイプを判定する。数字プレフィックスを除去して判定。"""
    # "010将軍" → "将軍", "022買い物相談" → "買い物相談"
    clean = re.sub(r'^\d+', '', channel_name).strip()

    mapping = {
        "将軍": "shogun",
        "書記丸": "ignore",
        "思い出し": "omoidashi",
        "家計": "kakeibo",
        "欲しいもの": "wishlist",
        "ほしいもの": "wishlist",
        "買い物相談": "shopping",
        "読書": "reading",
        "積読": "tsundoku",
        "読みたい本": "wantbook",
        "開発": "dev",
    }

    for key, value in mapping.items():
        if key in clean:
            return value

    # 通知チャンネル
    for nc in _NOTIFICATION_CHANNELS:
        if nc in channel_name:
            return "notification"

    # 無視チャンネル
    for ic in _IGNORE_CHANNELS:
        if ic in channel_name:
            return "ignore"

    return "unknown"


# =============================================================================
# 旅行ノート（書記丸用）
# =============================================================================

_TRIP_KEYWORDS = {
    "aomori": ("青森", "界 津軽", "弘前", "ねぶた", "浅虫", "津軽金山", "東横INN新青森"),
    "okinawa": ("沖縄", "石垣", "波照間", "西表", "ニシ浜", "うるま家", "うさぎや", "離島旅"),
}


def _load_trip_notes(content: str) -> str:
    """メッセージ内のキーワードに応じて共通トリップノートを読み込む。"""
    if not os.path.isdir(TRIPS_DIR):
        return ""
    loaded = []
    for trip_key, keywords in _TRIP_KEYWORDS.items():
        if not any(kw in content for kw in keywords):
            continue
        for fname in sorted(os.listdir(TRIPS_DIR)):
            if fname.startswith(".") or fname == "README.md":
                continue
            if trip_key not in fname.lower():
                continue
            fpath = os.path.join(TRIPS_DIR, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    loaded.append(f"=== {fname} ===\n{f.read()}")
            except OSError as e:
                logger.warning("トリップノート読み込み失敗 %s: %s", fpath, e)
    return "\n\n".join(loaded)


# =============================================================================
# チャンネル別ハンドラ
# =============================================================================

async def handle_shogun(message: discord.Message, clean: str):
    """#将軍: 汎用チャット。スレッド分離型+長期記憶。"""
    ctx_key = _get_context_key(message)

    # !new で履歴リセット
    if clean.strip().lower() in ("!new", "!reset", "!clear"):
        # 現在の会話をクローズ（要約生成）
        history = _get_history(ctx_key)
        if history:
            await _close_and_summarize(ctx_key, message.channel.name)
        _clear_history(ctx_key)
        await message.channel.send("会話をリセットしました。新しい話題をどうぞ。")
        return

    # 長期記憶から関連情報を検索
    memory_hits = await asyncio.to_thread(memory_search, clean)

    # コンテキスト付きプロンプト組み立て
    prompt = _build_prompt_with_context(
        ctx_key, clean,
        system_prefix="あなたはDaNARU幕府の将軍です。上様（代表）の質問に簡潔に回答してください。",
        memory_hits=memory_hits,
    )

    await message.channel.send("考え中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    # 履歴に追加
    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])




def _load_diary_cache() -> dict:
    """diary_cache.jsonを読み込んで返す。なければ空dict。"""
    cache_path = os.path.join(COMPANY_DIR, "80_ナレッジ", "my-daily-note", "diary_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_dev_log(days: int = 14) -> str:
    """git logから直近N日の開発記録を取得。日付ごとにグルーピングして返す。"""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={days} days ago",
             "--format=%ad|%s", "--date=short"],
            capture_output=True, text=True, timeout=10,
            cwd=COMPANY_DIR,
        )
        if not result.stdout.strip():
            return ""
        # 日付ごとにグルーピング
        by_date: dict = {}
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            date_str, msg = line.split("|", 1)
            date_str = date_str.strip()
            msg = msg.strip()
            # heartbeat/bakufu自動コミットは除外（量が多すぎるため）
            if any(kw in msg for kw in ("heartbeat(", "bakufu(", "followup巡回", "heart")):
                continue
            by_date.setdefault(date_str, []).append(msg)
        if not by_date:
            return ""
        lines = []
        for date_str in sorted(by_date.keys(), reverse=True):
            msgs = by_date[date_str]
            lines.append(f"{date_str}: " + " / ".join(msgs[:3]))  # 1日最大3件
        return "\n".join(lines[:10])  # 最大10日分
    except Exception as e:
        logger.warning("dev_log取得失敗: %s", e)
        return ""


def _load_schedule() -> str:
    """schedule.jsonから直近のイベントを読み込み文字列に変換。"""
    schedule_path = os.path.join(COMPANY_DIR, "80_ナレッジ", "my-daily-note", "schedule.json")
    try:
        with open(schedule_path, encoding="utf-8") as f:
            events = json.load(f)
        today = datetime.now().date()
        upcoming = []
        for ev in events:
            try:
                ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                days_until = (ev_date - today).days
                if -1 <= days_until <= 60:  # 昨日〜60日後
                    label = "今日" if days_until == 0 else (f"あと{days_until}日" if days_until > 0 else "昨日")
                    upcoming.append(f"・{ev['date']} ({label}) — {ev['title']}")
            except Exception:
                pass
        return "\n".join(upcoming) if upcoming else ""
    except Exception:
        return ""


def _build_diary_context(cache: dict) -> str:
    """3層日記コンテキストを文字列に変換。"""
    parts = []

    # Layer 3: 人物像DB
    persona = cache.get("persona", "")
    if persona and len(persona) > 20:
        parts.append(f"## 上様の人物像（累積分析）\n{persona}")

    # Layer 2: 月次サマリー（直近3ヶ月）
    monthly = cache.get("monthly_summaries", {})
    if monthly:
        recent_months = sorted(monthly.keys(), reverse=True)[:3]
        summaries = []
        for ym in recent_months:
            s = monthly[ym]
            if s and "日記なし" not in s and len(s) > 20:
                summaries.append(f"【{ym}】{s}")
        if summaries:
            parts.append("## 直近3ヶ月のサマリー\n" + "\n".join(summaries))

    # Layer 1: 直近3日の日記全文
    recent_index = cache.get("recent_7days_index", [])
    if recent_index:
        daily_dir = os.path.join(COMPANY_DIR, "80_ナレッジ", "my-daily-note", "02_Daily（デイリーノート用）")
        recent_texts = []
        for date_str in recent_index[:3]:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                fpath = os.path.join(daily_dir, d.strftime("%Y"), d.strftime("%Y%m"), f"{date_str}.md")
                with open(fpath, encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        recent_texts.append(f"=== {date_str} ===\n{content[:1000]}")
            except Exception:
                pass
        if recent_texts:
            parts.append("## 直近の日記\n" + "\n\n".join(recent_texts))

    return "\n\n".join(parts)


async def handle_omoidashi(message: discord.Message, clean: str):
    """#思い出し: メンター。日記3層分析＋スケジュール＋書庫から助言。"""
    ctx_key = _get_context_key(message)

    # 並列データ取得
    cache_task = asyncio.to_thread(_load_diary_cache)
    schedule_task = asyncio.to_thread(_load_schedule)
    books_task = asyncio.to_thread(_super_app_get, "books")
    trips_task = asyncio.to_thread(_super_app_get, "trips")
    dev_log_task = asyncio.to_thread(_load_dev_log, 14)

    cache, schedule_text, books_data, trips_data, dev_log_text = await asyncio.gather(
        cache_task, schedule_task, books_task, trips_task, dev_log_task
    )

    diary_context = _build_diary_context(cache)

    # 読書中の本
    books_section = ""
    if books_data and isinstance(books_data, list):
        reading = [b for b in books_data if b.get("status") == "reading"]
        if reading:
            books_section = "読書中: " + "、".join(b.get("title", "?") for b in reading[:5])

    # 直近・今後の旅行
    trips_section = ""
    if trips_data and isinstance(trips_data, list):
        today_str = datetime.now().strftime("%Y-%m-%d")
        upcoming_trips = []
        for t in trips_data:
            if t.get("end_date", "") >= today_str:
                days = (datetime.strptime(t["start_date"], "%Y-%m-%d") - datetime.now()).days
                label = "進行中" if days <= 0 else f"あと{days}日"
                upcoming_trips.append(f"・{t['start_date']}〜{t['end_date']} {t['name']} ({label})")
        if upcoming_trips:
            trips_section = "旅行予定:\n" + "\n".join(upcoming_trips)

    # システムプロンプト組み立て
    context_parts = []
    if diary_context:
        context_parts.append(diary_context)
    if dev_log_text:
        context_parts.append(f"## 直近の開発記録（git log）\n{dev_log_text}")
    if schedule_text:
        context_parts.append(f"## 直近のスケジュール\n{schedule_text}")
    if trips_section:
        context_parts.append(trips_section)
    if books_section:
        context_parts.append(books_section)

    context_block = "\n\n".join(context_parts)

    system = f"""あなたは上様（下田成大）の専属メンターです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。

以下の日記・開発記録・スケジュール情報をもとに、上様の状況を深く理解した上で会話してください。

{context_block}

---
応答のスタイル:
- 心理カウンセラーのように傾聴する。説教しない
- 日記の具体的な記述（昨日何をしたか、どんな気持ちだったか）を拾って言及する
- 日記と開発記録を照らし合わせて「気持ちが低調だったのにこの日はこれを完成させてたんですね」のように連動を見つけて語る
- 直近のスケジュールや旅行予定があれば自然に触れる（例:「G検定まであとN日ですね」）
- 「あなたらしい」と感じさせる言葉かけ。人物像DBのパターンを活かす
- 回答は短め（5〜8行程度）。まず共感、次に一言の問いかけ"""

    # memory_hitsは使わない（プロジェクト記憶が混入するため）
    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)

    await message.channel.send("少し考えさせてください...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_kakeibo(message: discord.Message, clean: str):
    """#家計: 家計全般+予算相談。"""
    ctx_key = _get_context_key(message)

    # スーパーアプリから家計データ取得
    kakeibo_data = await asyncio.to_thread(_super_app_get, "kakeibo")
    monthly_total = 0
    if kakeibo_data and isinstance(kakeibo_data, list):
        this_month = datetime.now().strftime("%Y-%m")
        for entry in kakeibo_data:
            if entry.get("date", "").startswith(this_month) and entry.get("type") == "expense":
                monthly_total += entry.get("amount", 0)

    system = f"""あなたはDaNARU幕府の家計アドバイザーです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。
今月の支出合計: ¥{monthly_total:,}

上様の家計相談に回答してください。節約アドバイスは押し付けず、聞かれたら答える程度に。
データをスーパーアプリに記録する必要がある場合は、記録した旨を伝えてください。"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)

    # 収支データの自動記録
    data_keywords = ("買った", "購入", "支出", "収入", "円使った", "円払った", "記録して")
    if any(kw in clean for kw in data_keywords):
        saved = await asyncio.to_thread(_extract_and_save_kakeibo, clean)
        if saved:
            await message.channel.send(saved)
            _add_history(ctx_key, "user", clean)
            _add_history(ctx_key, "assistant", saved)
            return

    await message.channel.send("家計を確認中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_wishlist(message: discord.Message, clean: str):
    """#ほしいもの: 欲しいものメモ + 会話対応。"""
    ctx_key = _get_context_key(message)

    # 1. 登録キーワード → 抽出して保存
    register_keywords = ("欲しい", "ほしい", "買いたい", "気になる", "追加", "登録", "円")
    if any(kw in clean for kw in register_keywords):
        extract_prompt = f"""以下のメッセージから欲しいものを抽出してJSONで返してください。
該当しない場合は {{"type": "none"}} を返してください。

フォーマット: {{"type":"wishlist","name":"商品名","price":金額,"priority":"high|medium|low","category":"gadget|outdoor|book|clothing|home|service|other","memo":"メモ"}}
金額がない場合は0。優先度が不明ならmedium。

今日は{datetime.now().strftime('%Y-%m-%d')}です。
メッセージ: {clean}

JSONのみ返してください。"""

        result = await asyncio.to_thread(run_claude, extract_prompt, 30)
        try:
            match = re.search(r'\{[^}]+\}', result)
            if match:
                data = json.loads(match.group())
                if data.get("type") == "wishlist":
                    data["source"] = "discord"
                    resp = await asyncio.to_thread(_super_app_post, "wishlist", "", data)
                    if resp:
                        price_str = f" ¥{data.get('price', 0):,}" if data.get("price") else ""
                        await message.channel.send(f"登録しました: {data.get('name', '?')}{price_str} [{data.get('priority', 'medium')}]")
                        existing = await asyncio.to_thread(_super_app_get, "wishlist")
                        if existing and isinstance(existing, list) and len(existing) > 1:
                            total = sum(item.get("price", 0) for item in existing)
                            await message.channel.send(f"欲しいものリスト: {len(existing)}件 / 合計 ¥{total:,}")
                        return
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. リスト表示のみ要求
    list_keywords = ("リスト", "一覧", "見せて", "何件", "全部")
    if any(kw in clean for kw in list_keywords) and len(clean) < 20:
        existing = await asyncio.to_thread(_super_app_get, "wishlist")
        if existing and isinstance(existing, list):
            lines = [f"**欲しいものリスト ({len(existing)}件)**"]
            total = 0
            for item in existing[:15]:
                price = item.get("price", 0)
                total += price
                price_str = f"¥{price:,}" if price else "未定"
                lines.append(f"- {item.get('name', '?')} ({price_str}) [{item.get('priority', '?')}]")
            lines.append(f"\n合計: ¥{total:,}")
            await message.channel.send("\n".join(lines))
        else:
            await message.channel.send("欲しいものリストは空です。商品名を送ってください。")
        return

    # 3. それ以外 → LLMで会話応答
    existing = await asyncio.to_thread(_super_app_get, "wishlist")
    wishlist_summary = ""
    total = 0
    if existing and isinstance(existing, list):
        for item in existing:
            total += item.get("price", 0)
        wishlist_summary = "\n".join(
            f"- {w.get('name', '?')} ¥{w.get('price', 0):,} [{w.get('priority', '?')}]"
            for w in existing[:15]
        )

    kakeibo_data = await asyncio.to_thread(_super_app_get, "kakeibo")
    monthly_total = 0
    if kakeibo_data and isinstance(kakeibo_data, list):
        this_month = datetime.now().strftime("%Y-%m")
        for entry in kakeibo_data:
            if entry.get("date", "").startswith(this_month) and entry.get("type") == "expense":
                monthly_total += entry.get("amount", 0)

    system = f"""あなたはDaNARU幕府の欲しいものアドバイザーです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。

[欲しいものリスト ({len(existing or [])}件 / 合計¥{total:,})]
{wishlist_summary or '(なし)'}

[今月の支出] ¥{monthly_total:,}

上様の質問・相談に回答してください。
- 優先順位の相談 → 予算・必要性・緊急度を考慮
- 家計の状況と欲しいものの関係を俯瞰的に分析
- 代替品の提案、セール情報への言及も歓迎
- 簡潔に、でも根拠を添えて"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    await message.channel.send("考え中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_shopping(message: discord.Message, clean: str):
    """#買い物相談: 購入意思決定+レシート+購入後記録。"""
    ctx_key = _get_context_key(message)

    # 欲しいものリストと家計データを取得
    wishlist_data = await asyncio.to_thread(_super_app_get, "wishlist")
    kakeibo_data = await asyncio.to_thread(_super_app_get, "kakeibo")

    monthly_total = 0
    if kakeibo_data and isinstance(kakeibo_data, list):
        this_month = datetime.now().strftime("%Y-%m")
        for entry in kakeibo_data:
            if entry.get("date", "").startswith(this_month) and entry.get("type") == "expense":
                monthly_total += entry.get("amount", 0)

    wishlist_summary = ""
    if wishlist_data and isinstance(wishlist_data, list):
        wishlist_summary = "\n".join(
            f"- {w.get('name', '?')} ¥{w.get('price', 0):,} [{w.get('priority', '?')}]"
            for w in wishlist_data[:10]
        )

    system = f"""あなたは買い物アドバイザーです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。
今月の支出: ¥{monthly_total:,}

[欲しいものリスト]
{wishlist_summary or '(なし)'}

上様の買い物相談に回答してください。
- 買うべきか迷っている → 予算・必要性・優先度を踏まえてアドバイス
- レシートや購入報告 → 家計簿に記録
- 「買った」「購入した」→ 自動で家計記録
客観的に、でも最終判断は上様に委ねてください。"""

    # 購入記録の自動保存
    record_keywords = ("買った", "購入", "円使った", "円払った", "レシート", "記録")
    if any(kw in clean for kw in record_keywords):
        saved = await asyncio.to_thread(_extract_and_save_kakeibo, clean)
        if saved:
            await message.channel.send(saved)
            _add_history(ctx_key, "user", clean)
            _add_history(ctx_key, "assistant", saved)
            return

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    await message.channel.send("検討中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_reading(message: discord.Message, clean: str):
    """#読書: 本ごとにスレッド、感想・進捗。"""
    ctx_key = _get_context_key(message)

    books_data = await asyncio.to_thread(_super_app_get, "books")
    books_summary = ""
    if books_data and isinstance(books_data, list):
        for status in ["reading", "done", "backlog"]:
            filtered = [b for b in books_data if b.get("status") == status]
            if filtered:
                label = {"reading": "読書中", "done": "読了", "backlog": "積読"}[status]
                books_summary += f"\n[{label}] " + ", ".join(b.get("title", "?") for b in filtered[:5])

    system = f"""あなたは読書アドバイザーです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。
{books_summary}

上様の読書に関する話題に応答してください。感想の共有、ディスカッション、読書メモの記録を手伝ってください。"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    await message.channel.send("読書ノートを確認中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_tsundoku(message: discord.Message, clean: str):
    """#積読: 所有未読の管理。"""
    ctx_key = _get_context_key(message)

    # 全書籍データを取得
    all_books = await asyncio.to_thread(_super_app_get, "books") or []
    backlog = [b for b in all_books if b.get("status") == "backlog"]
    reading = [b for b in all_books if b.get("status") == "reading"]

    # 本の登録キーワード → 抽出して保存
    register_keywords = ("買った", "届いた", "もらった", "追加", "登録")
    if any(kw in clean for kw in register_keywords):
        extract_prompt = f"""以下から書籍情報を抽出してJSONで返してください。
{{"type":"book","title":"書名","author":"著者","price":金額,"status":"backlog"}}
該当しなければ {{"type":"none"}}。JSONのみ。

メッセージ: {clean}"""
        result = await asyncio.to_thread(run_claude, extract_prompt, 30)
        try:
            match = re.search(r'\{[^}]+\}', result)
            if match:
                data = json.loads(match.group())
                if data.get("type") == "book":
                    data["source"] = "discord"
                    resp = await asyncio.to_thread(_super_app_post, "books", "", data)
                    if resp:
                        await message.channel.send(f"積読に追加: {data.get('title', '?')}")
                        return
        except (json.JSONDecodeError, AttributeError):
            pass

    # リスト表示のみ要求
    list_only_keywords = ("リスト", "一覧", "見せて", "何冊")
    if any(kw in clean for kw in list_only_keywords) and len(clean) < 20:
        lines = [f"**積読リスト ({len(backlog)}冊)**"]
        for b in backlog[:20]:
            lines.append(f"- {b.get('title', '?')} {b.get('author', '')}")
        await message.channel.send("\n".join(lines))
        return

    # それ以外（会話・相談・提案依頼など）→ LLMで応答
    backlog_titles = "\n".join(f"- {b.get('title','?')} / {b.get('author','')}" for b in backlog)
    reading_titles = "\n".join(f"- {b.get('title','?')} / {b.get('author','')}" for b in reading)

    # PDFで持っている本を確認
    pdf_dir = os.path.join(COMPANY_DIR, "80_ナレッジ", "my-daily-note", "08_書籍")
    pdf_books = []
    try:
        for f in os.listdir(pdf_dir):
            if f.endswith(".pdf"):
                pdf_books.append(f.replace(".pdf", ""))
    except Exception:
        pass
    pdf_section = "\n".join(f"- {p}" for p in pdf_books[:20]) if pdf_books else "（なし）"

    system = f"""あなたは上様の読書管理アシスタントです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。

【積読リスト ({len(backlog)}冊)】
{backlog_titles}

【読書中 ({len(reading)}冊)】
{reading_titles}

【PDFで既に持っている書籍】
{pdf_section}

上様の読書相談・質問に答えてください。
- 本の優先順位を聞かれたら、auto-trade・G検定・事業への直結度を考慮して提案
- PDFは「AIが要約してくれる」前提なので、物理本の優先候補から外してよい
- 借り物の本（返却期限あり）は優先度を上げる
- 返答は具体的で簡潔に"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_wantbook(message: discord.Message, clean: str):
    """#読みたい本: 購入候補・優先度付け + 会話対応。"""
    ctx_key = _get_context_key(message)

    # 1. 登録キーワード → 抽出して保存
    register_keywords = ("読みたい", "気になる", "おすすめ", "追加", "登録", "買いたい")
    if any(kw in clean for kw in register_keywords):
        extract_prompt = f"""以下から書籍情報を抽出してJSONで返してください。
{{"type":"book","title":"書名","author":"著者","price":金額,"status":"want"}}
該当しなければ {{"type":"none"}}。JSONのみ。

メッセージ: {clean}"""
        result = await asyncio.to_thread(run_claude, extract_prompt, 30)
        try:
            match = re.search(r'\{[^}]+\}', result)
            if match:
                data = json.loads(match.group())
                if data.get("type") == "book":
                    data["source"] = "discord"
                    resp = await asyncio.to_thread(_super_app_post, "books", "", data)
                    if resp:
                        price_str = f" ¥{data.get('price', 0):,}" if data.get("price") else ""
                        await message.channel.send(f"読みたい本に追加: {data.get('title', '?')}{price_str}")
                        return
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. リスト表示のみ要求
    list_keywords = ("リスト", "一覧", "見せて", "何冊", "全部")
    if any(kw in clean for kw in list_keywords) and len(clean) < 20:
        books_data = await asyncio.to_thread(_super_app_get, "books", "?status=want")
        if books_data and isinstance(books_data, list):
            lines = [f"**読みたい本リスト ({len(books_data)}冊)**"]
            total = sum(b.get("price", 0) for b in books_data)
            for b in books_data[:15]:
                price = f"¥{b.get('price', 0):,}" if b.get("price") else "未定"
                lines.append(f"- {b.get('title', '?')} ({price})")
            lines.append(f"\n合計: ¥{total:,}")
            await message.channel.send("\n".join(lines))
        else:
            await message.channel.send("読みたい本リストは空です。書籍名を送ってください。")
        return

    # 3. それ以外 → LLMで会話応答
    all_books = await asyncio.to_thread(_super_app_get, "books") or []
    want = [b for b in all_books if b.get("status") == "want"]
    backlog = [b for b in all_books if b.get("status") == "backlog"]
    reading = [b for b in all_books if b.get("status") == "reading"]

    want_list = "\n".join(f"- {b.get('title','?')} / {b.get('author','')}" for b in want) or "（なし）"
    backlog_list = "\n".join(f"- {b.get('title','?')}" for b in backlog[:10]) or "（なし）"
    reading_list = "\n".join(f"- {b.get('title','?')}" for b in reading) or "（なし）"

    system = f"""あなたはDaNARU幕府の読書購入アドバイザーです。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。

[読みたい本 ({len(want)}冊)]
{want_list}

[積読 ({len(backlog)}冊)]
{backlog_list}

[読書中 ({len(reading)}冊)]
{reading_list}

上様の質問・相談に回答してください。
- 積読が多い場合は購入を慎重に勧める
- auto-trade・G検定・事業への直結度で優先順位を提案
- 「次に買うべき1冊」を聞かれたら明確に答える
- 簡潔に、根拠を添えて"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    await message.channel.send("考え中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


async def handle_dev(message: discord.Message, clean: str):
    """#開発: Discord経由のエラー修正・開発。"""
    ctx_key = _get_context_key(message)

    system = f"""あなたはDaNARU幕府の開発担当です。
今日は{datetime.now().strftime('%Y年%m月%d日')}です。
作業ディレクトリ: {COMPANY_DIR}

上様からのエラー報告や開発依頼に対応してください。
ただしDiscord経由では実際のコード修正はできません。
問題の診断、解決策の提案、次のアクション（「Claude Codeで〇〇を実行してください」等）を回答してください。"""

    prompt = _build_prompt_with_context(ctx_key, clean, system_prefix=system)
    await message.channel.send("調査中...")
    result = await asyncio.to_thread(run_claude, prompt, 120)
    await message.channel.send(result)

    _add_history(ctx_key, "user", clean)
    _add_history(ctx_key, "assistant", result[:500])


# =============================================================================
# 家計記録抽出
# =============================================================================

def _extract_and_save_kakeibo(content: str) -> Optional[str]:
    """メッセージから収支データを抽出してスーパーアプリに保存する。"""
    prompt = f"""以下のメッセージから収支データを抽出してJSONで返してください。
該当しない場合は {{"type": "none"}} を返してください。

フォーマット:
支出: {{"type":"expense","amount":金額,"category":"food|daily|transport|entertainment|health|education|clothing|housing|subscription|other","memo":"内容","date":"YYYY-MM-DD"}}
収入: {{"type":"income","amount":金額,"category":"salary|freelance|investment|other","memo":"内容","date":"YYYY-MM-DD"}}

今日は{datetime.now().strftime('%Y-%m-%d')}です。金額がない場合は0、日付がない場合は今日。
メッセージ: {content}

JSONのみ返してください。"""

    result = run_claude(prompt, 30)
    try:
        match = re.search(r'\{[^}]+\}', result)
        if not match:
            return None
        data = json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        return None

    dtype = data.get("type", "none")
    if dtype == "none":
        return None

    data["source"] = "discord"
    resp = _super_app_post("kakeibo", "", data)
    if resp:
        sign = "-" if dtype == "expense" else "+"
        return f"家計記録: {sign}¥{data.get('amount', 0):,} ({data.get('memo', '')})"
    return None


# =============================================================================
# 会話クローズ+要約生成
# =============================================================================

async def _close_and_summarize(ctx_key: str, channel_name: str):
    """現在の会話履歴から要約を生成し、長期記憶DBに保存する。"""
    history = _get_history(ctx_key)
    if not history or len(history) < 2:
        return

    # 会話内容を文字列化
    conv_text = "\n".join(
        f"{'上様' if m['role'] == 'user' else '将軍'}: {m['content']}"
        for m in history
    )

    # 要約+キーワード生成
    summary_prompt = f"""以下の会話を要約してJSONで返してください。
{{"summary": "200字以内の要約", "keywords": "カンマ区切りのキーワード5つ以内"}}

会話:
{conv_text[:3000]}

JSONのみ返してください。"""

    result = await asyncio.to_thread(run_claude, summary_prompt, 30)
    try:
        match = re.search(r'\{[^}]+\}', result)
        if match:
            data = json.loads(match.group())
            conv_id = await asyncio.to_thread(
                memory_create_conversation, ctx_key, channel_name
            )
            if conv_id:
                await asyncio.to_thread(
                    memory_close_conversation, conv_id,
                    data.get("summary", ""),
                    data.get("keywords", ""),
                )
                logger.info("会話要約保存: %s (keywords: %s)", conv_id, data.get("keywords", ""))
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning("要約生成失敗: %s", e)


# =============================================================================
# メインイベントハンドラ
# =============================================================================

@client.event
async def on_ready():
    logger.info("Discord Bot v2 ログイン完了: %s (ID: %s)", client.user, client.user.id)



@client.event
async def on_message(message: discord.Message):
    # 自分のメッセージには反応しない
    if message.author == client.user:
        return
    # Bot以外のBotメッセージも無視
    if message.author.bot:
        return

    # === 書記丸チャンネル最終防衛ライン（あらゆるキャッシュ問題を回避） ===
    ch = message.channel
    _names = []
    if hasattr(ch, "name") and ch.name:
        _names.append(ch.name)
    if hasattr(ch, "parent") and ch.parent and hasattr(ch.parent, "name"):
        _names.append(ch.parent.name)
    if hasattr(ch, "parent_id") and ch.parent_id:
        _p = client.get_channel(ch.parent_id)
        if _p and hasattr(_p, "name"):
            _names.append(_p.name)
    if hasattr(ch, "category") and ch.category and hasattr(ch.category, "name"):
        _names.append(ch.category.name)
    if any("書記丸" in n for n in _names):
        return

    content = message.content.strip()
    if not content:
        return

    # チャンネル名を取得
    if isinstance(message.channel, discord.DMChannel):
        channel_name = "dm"
        channel_type = "shogun"  # DMは将軍扱い
    elif isinstance(message.channel, discord.Thread):
        parent = message.channel.parent
        channel_name = parent.name if parent else "unknown"
        channel_type = _get_channel_type(channel_name)
    else:
        channel_name = message.channel.name
        channel_type = _get_channel_type(channel_name)

    logger.info("受信: [%s/%s] %s: %s", channel_type, channel_name, message.author, content[:50])

    # 通知・無視チャンネルはスキップ
    if channel_type in ("notification", "ignore"):
        return

    # メンション除去
    clean = re.sub(r"<@!?\d+>", "", content).strip()
    if not clean:
        return

    # 専用チャンネルではメンション不要で反応
    # unknownチャンネルではメンション or DM必須
    if channel_type == "unknown":
        is_mentioned = client.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)
        if not is_mentioned and not is_dm:
            return

    # チャンネル別ルーティング
    handlers = {
        "shogun": handle_shogun,
        "omoidashi": handle_omoidashi,
        "kakeibo": handle_kakeibo,
        "wishlist": handle_wishlist,
        "shopping": handle_shopping,
        "reading": handle_reading,
        "tsundoku": handle_tsundoku,
        "wantbook": handle_wantbook,
        "dev": handle_dev,
    }

    handler = handlers.get(channel_type)
    if handler:
        try:
            await handler(message, clean)
        except Exception as e:
            logger.error("ハンドラ例外 [%s]: %s", channel_type, e)
            await message.channel.send(f"(エラーが発生しました: {e})")
    else:
        # フォールバック: 将軍として応答
        await handle_shogun(message, clean)


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("DISCORD_BOT_TOKEN を .env に設定してください。")
        sys.exit(1)
    client.run(BOT_TOKEN, log_handler=None)

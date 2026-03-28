#!/usr/bin/env python3
"""
sentiment_monitor.py — ニュース・センチメント分析モニター

RSSフィードからニュースを取得し、ルールベースでセンチメントスコアを算出。
重要な速報はDiscord通知する。

使い方:
    python3 sentiment_monitor.py              # 1回実行
    python3 sentiment_monitor.py --loop       # 30分間隔で常駐
    python3 sentiment_monitor.py --keyword BTC  # キーワード指定

設計:
    - 複数RSSフィード（Reuters, CNBC, CoinDesk, 日経等）を巡回
    - 各記事をキーワードマッチ → センチメントスコア(-1.0〜+1.0)
    - 高スコア or 高インパクトキーワードを含む記事はDiscord通知
    - 過去通知の重複チェック（タイトルハッシュ）
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from notifier import send_discord_embed

# --- RSSフィード一覧 ---
RSS_FEEDS = [
    # 英語圏（グローバル金融）
    {"name": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "lang": "en"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "lang": "en"},
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "lang": "en"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "lang": "en"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss", "lang": "en"},
    # 日本語
    {"name": "日経 マーケット", "url": "https://assets.wor.jp/rss/rdf/nikkei/markets.rdf", "lang": "ja"},
    {"name": "ロイター日本語", "url": "https://assets.wor.jp/rss/rdf/reuters/top.rdf", "lang": "ja"},
]

# --- センチメント辞書 ---
# 各キーワードに重み（正=ポジティブ、負=ネガティブ）とインパクト（1-3）を設定

SENTIMENT_KEYWORDS = {
    # === 超高インパクト（即時通知レベル）===
    # 英語
    "rate cut": {"score": 0.8, "impact": 3},
    "rate hike": {"score": -0.6, "impact": 3},
    "fed pivot": {"score": 0.9, "impact": 3},
    "recession": {"score": -0.8, "impact": 3},
    "bank run": {"score": -1.0, "impact": 3},
    "default": {"score": -0.9, "impact": 3},
    "bankruptcy": {"score": -0.9, "impact": 3},
    "sanctions": {"score": -0.7, "impact": 3},
    "war": {"score": -0.8, "impact": 3},
    "ceasefire": {"score": 0.7, "impact": 3},
    "hack": {"score": -0.8, "impact": 3},
    "exploit": {"score": -0.7, "impact": 3},
    "private placement": {"score": 0.5, "impact": 3},  # 私募（友人が言及した例）
    "etf approved": {"score": 0.9, "impact": 3},
    "etf rejected": {"score": -0.8, "impact": 3},
    "halving": {"score": 0.6, "impact": 3},

    # 日本語
    "利下げ": {"score": 0.8, "impact": 3},
    "利上げ": {"score": -0.6, "impact": 3},
    "金融緩和": {"score": 0.7, "impact": 3},
    "金融引き締め": {"score": -0.6, "impact": 3},
    "景気後退": {"score": -0.8, "impact": 3},
    "デフォルト": {"score": -0.9, "impact": 3},
    "破綻": {"score": -0.9, "impact": 3},
    "制裁": {"score": -0.7, "impact": 3},
    "戦争": {"score": -0.8, "impact": 3},
    "停戦": {"score": 0.7, "impact": 3},
    "ハッキング": {"score": -0.8, "impact": 3},
    "私募": {"score": 0.5, "impact": 3},
    "上場": {"score": 0.6, "impact": 3},

    # === 高インパクト ===
    "rally": {"score": 0.6, "impact": 2},
    "surge": {"score": 0.7, "impact": 2},
    "soar": {"score": 0.7, "impact": 2},
    "bull": {"score": 0.5, "impact": 2},
    "plunge": {"score": -0.7, "impact": 2},
    "crash": {"score": -0.9, "impact": 2},
    "sell-off": {"score": -0.7, "impact": 2},
    "bear": {"score": -0.5, "impact": 2},
    "inflation": {"score": -0.4, "impact": 2},
    "tariff": {"score": -0.5, "impact": 2},
    "stimulus": {"score": 0.6, "impact": 2},
    "earnings beat": {"score": 0.6, "impact": 2},
    "earnings miss": {"score": -0.6, "impact": 2},
    "upgrade": {"score": 0.5, "impact": 2},
    "downgrade": {"score": -0.5, "impact": 2},
    "all-time high": {"score": 0.7, "impact": 2},
    "record high": {"score": 0.6, "impact": 2},

    "急騰": {"score": 0.7, "impact": 2},
    "急落": {"score": -0.7, "impact": 2},
    "暴落": {"score": -0.9, "impact": 2},
    "最高値": {"score": 0.6, "impact": 2},
    "インフレ": {"score": -0.4, "impact": 2},
    "関税": {"score": -0.5, "impact": 2},
    "決算 好調": {"score": 0.5, "impact": 2},
    "決算 不振": {"score": -0.5, "impact": 2},
    "格上げ": {"score": 0.5, "impact": 2},
    "格下げ": {"score": -0.5, "impact": 2},

    # === 中インパクト ===
    "growth": {"score": 0.3, "impact": 1},
    "decline": {"score": -0.3, "impact": 1},
    "uncertainty": {"score": -0.3, "impact": 1},
    "volatility": {"score": -0.2, "impact": 1},
    "recovery": {"score": 0.4, "impact": 1},
    "上昇": {"score": 0.3, "impact": 1},
    "下落": {"score": -0.3, "impact": 1},
    "回復": {"score": 0.4, "impact": 1},
    "不透明": {"score": -0.3, "impact": 1},
}

# 市場関連キーワード → 関連市場のマッピング
MARKET_KEYWORDS = {
    "bitcoin": "btc", "btc": "btc", "crypto": "btc", "ethereum": "btc",
    "ビットコイン": "btc", "仮想通貨": "btc", "暗号資産": "btc",
    "gold": "gold", "ゴールド": "gold", "金価格": "gold",
    "oil": "commodity", "crude": "commodity", "原油": "commodity",
    "nikkei": "jp", "日経": "jp", "東証": "jp", "topix": "jp",
    "s&p": "us", "nasdaq": "us", "dow": "us", "nyse": "us",
    "ドル円": "fx", "usdjpy": "fx", "forex": "fx", "為替": "fx",
    "fed": "us", "fomc": "us", "日銀": "jp", "boj": "jp",
}

POLL_INTERVAL_SEC = 30 * 60  # 30分
SEEN_ARTICLES_PATH = os.path.join(BASE_DIR, "sentiment_seen.json")
SENTIMENT_LOG_PATH = os.path.join(BASE_DIR, "sentiment_log.json")


def load_seen():
    if os.path.exists(SEEN_ARTICLES_PATH):
        try:
            with open(SEEN_ARTICLES_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_seen(seen):
    # 古いエントリーを削除（7日以上前）
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    cleaned = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_ARTICLES_PATH, "w") as f:
        json.dump(cleaned, f, indent=2)


def article_hash(title):
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:12]


def fetch_rss(feed):
    """RSSフィードを取得してパース"""
    articles = []
    try:
        resp = requests.get(feed["url"], timeout=15, headers={
            "User-Agent": "DaNARU-SentimentMonitor/1.0"
        })
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        # RSS 2.0 / RDF 形式両対応
        ns = {"rdf": "http://purl.org/rss/1.0/", "dc": "http://purl.org/dc/elements/1.1/"}
        items = root.findall(".//item") or root.findall(".//rdf:item", ns)

        for item in items[:20]:  # 最大20記事
            title_el = item.find("title")
            desc_el = item.find("description")
            link_el = item.find("link")
            pub_el = item.find("pubDate") or item.find("dc:date", ns)

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""

            if not title:
                continue

            # HTMLタグ除去
            desc = re.sub(r"<[^>]+>", "", desc)

            articles.append({
                "title": title,
                "description": desc[:300],
                "link": link,
                "source": feed["name"],
                "lang": feed["lang"],
            })

    except Exception as e:
        print(f"  [WARN] {feed['name']} 取得失敗: {e}")

    return articles


def analyze_sentiment(article):
    """記事のセンチメントスコアを算出"""
    text = (article["title"] + " " + article["description"]).lower()

    total_score = 0.0
    max_impact = 0
    matched_keywords = []

    for keyword, meta in SENTIMENT_KEYWORDS.items():
        kw_lower = keyword.lower()
        # 英語キーワードは単語境界でマッチ（部分一致を防ぐ）
        if kw_lower.isascii():
            pattern = r'\b' + re.escape(kw_lower) + r'\b'
            if not re.search(pattern, text):
                continue
        else:
            # 日本語キーワードは部分一致でOK
            if kw_lower not in text:
                continue
        total_score += meta["score"]
        max_impact = max(max_impact, meta["impact"])
        matched_keywords.append(keyword)

    # 関連市場を特定
    markets = set()
    for kw, market in MARKET_KEYWORDS.items():
        if kw.lower() in text:
            markets.add(market)

    # スコアを -1.0 〜 +1.0 に正規化
    if matched_keywords:
        normalized_score = max(-1.0, min(1.0, total_score / len(matched_keywords)))
    else:
        normalized_score = 0.0

    return {
        "score": normalized_score,
        "impact": max_impact,
        "keywords": matched_keywords,
        "markets": list(markets),
    }


def should_notify(sentiment):
    """通知すべきかどうか判定"""
    # インパクト3（超高）は常に通知
    if sentiment["impact"] >= 3:
        return True
    # インパクト2 かつ スコアの絶対値が0.5以上
    if sentiment["impact"] >= 2 and abs(sentiment["score"]) >= 0.5:
        return True
    return False


def log_sentiment(article, sentiment):
    """センチメント結果をログに記録"""
    log = []
    if os.path.exists(SENTIMENT_LOG_PATH):
        try:
            with open(SENTIMENT_LOG_PATH) as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []

    log.append({
        "timestamp": datetime.now().isoformat(),
        "title": article["title"],
        "source": article["source"],
        "score": sentiment["score"],
        "impact": sentiment["impact"],
        "keywords": sentiment["keywords"],
        "markets": sentiment["markets"],
    })

    # 最新500件のみ保持
    log = log[-500:]

    with open(SENTIMENT_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def run_scan(keyword_filter=None, verbose=True):
    """全RSSフィードをスキャンしてセンチメント分析"""
    seen = load_seen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    results = []
    notified = 0

    if verbose:
        print(f"\n{'='*60}")
        print(f"  センチメントモニター  {now}")
        print(f"{'='*60}")

    for feed in RSS_FEEDS:
        if verbose:
            print(f"\n  [{feed['name']}] 取得中...")

        articles = fetch_rss(feed)
        new_articles = 0

        for article in articles:
            h = article_hash(article["title"])

            # 既読チェック
            if h in seen:
                continue

            # キーワードフィルター
            if keyword_filter:
                text = (article["title"] + " " + article["description"]).lower()
                if keyword_filter.lower() not in text:
                    continue

            seen[h] = datetime.now().isoformat()
            new_articles += 1

            # センチメント分析
            sentiment = analyze_sentiment(article)

            if sentiment["keywords"]:
                results.append({"article": article, "sentiment": sentiment})
                log_sentiment(article, sentiment)

                score_str = f"{sentiment['score']:+.2f}"
                impact_str = "*" * sentiment["impact"]

                if verbose:
                    print(f"    {impact_str} [{score_str}] {article['title'][:60]}")
                    if sentiment["keywords"]:
                        print(f"       キーワード: {', '.join(sentiment['keywords'][:5])}")

                # Discord通知
                if should_notify(sentiment):
                    emoji = "🟢" if sentiment["score"] > 0 else "🔴" if sentiment["score"] < 0 else "⚪"
                    color = 0x00FF00 if sentiment["score"] > 0 else 0xFF0000 if sentiment["score"] < 0 else 0x808080

                    fields = [
                        {"name": "スコア", "value": f"{emoji} {score_str}", "inline": True},
                        {"name": "インパクト", "value": impact_str, "inline": True},
                        {"name": "キーワード", "value": ", ".join(sentiment["keywords"][:5]), "inline": True},
                    ]
                    if sentiment["markets"]:
                        fields.append({
                            "name": "関連市場",
                            "value": ", ".join(sentiment["markets"]),
                            "inline": True,
                        })
                    if article["link"]:
                        fields.append({
                            "name": "リンク",
                            "value": article["link"][:100],
                            "inline": False,
                        })

                    send_discord_embed(
                        title=f"[SENTIMENT] {article['title'][:80]}",
                        description=article["description"][:200],
                        color=color,
                        fields=fields,
                        username="sentiment-monitor",
                    )
                    notified += 1

        if verbose:
            print(f"    新着: {new_articles}件")

    save_seen(seen)

    if verbose:
        print(f"\n  分析結果: {len(results)}件マッチ, {notified}件通知")
        print()

    return results


def get_market_sentiment(market=None):
    """直近のセンチメントログから市場別の平均スコアを返す（他モジュール連携用）"""
    if not os.path.exists(SENTIMENT_LOG_PATH):
        return {"score": 0.0, "count": 0, "trend": "neutral"}

    try:
        with open(SENTIMENT_LOG_PATH) as f:
            log = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"score": 0.0, "count": 0, "trend": "neutral"}

    # 直近24時間のエントリーのみ
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    recent = [e for e in log if e["timestamp"] > cutoff]

    if market:
        recent = [e for e in recent if market in e.get("markets", [])]

    if not recent:
        return {"score": 0.0, "count": 0, "trend": "neutral"}

    avg_score = sum(e["score"] for e in recent) / len(recent)
    trend = "bullish" if avg_score > 0.2 else "bearish" if avg_score < -0.2 else "neutral"

    return {
        "score": round(avg_score, 3),
        "count": len(recent),
        "trend": trend,
    }


def main():
    parser = argparse.ArgumentParser(description="ニュース・センチメント分析モニター")
    parser.add_argument("--loop", action="store_true", help="30分間隔で常駐実行")
    parser.add_argument("--keyword", type=str, help="キーワードフィルター")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC,
                        help=f"ポーリング間隔（秒、デフォルト{POLL_INTERVAL_SEC}）")
    parser.add_argument("--market-score", type=str,
                        help="市場別センチメントスコアを表示（btc/us/jp/fx/gold）")
    args = parser.parse_args()

    if args.market_score:
        result = get_market_sentiment(args.market_score)
        print(f"\n  [{args.market_score.upper()}] センチメント")
        print(f"    スコア: {result['score']:+.3f}")
        print(f"    記事数: {result['count']}件（24h）")
        print(f"    トレンド: {result['trend']}")
        return

    if args.loop:
        print(f"常駐モード開始（{args.interval}秒間隔）")
        while True:
            try:
                run_scan(keyword_filter=args.keyword)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n終了")
                break
            except Exception as e:
                print(f"  [ERROR] スキャンエラー: {e}")
                time.sleep(60)
    else:
        run_scan(keyword_filter=args.keyword)


if __name__ == "__main__":
    main()

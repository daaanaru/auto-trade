"""
jquants_fetcher.py — JQuants API で日本株データを取得する

yfinance の日本株データが不安定（YFRateLimitError頻発）な問題の代替。
東証公式データなので精度・安定性が高い。

認証方法（どちらか一方を .env に設定）:
    JQUANTS_REFRESH_TOKEN=xxx   ← 推奨（JQuantsダッシュボードから取得）
    または
    JQUANTS_EMAIL=your@email.com
    JQUANTS_PASSWORD=yourpassword

無料プランの制限:
    - 前日までのデータ（当日リアルタイムは不可）
    - 東証全銘柄の株価OHLCV + 財務情報
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# .envから環境変数を読み込む
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

JQUANTS_REFRESH_TOKEN = os.environ.get("JQUANTS_REFRESH_TOKEN", "")
JQUANTS_EMAIL = os.environ.get("JQUANTS_EMAIL", "")
JQUANTS_PASSWORD = os.environ.get("JQUANTS_PASSWORD", "")

_client = None


def _get_client():
    """JQuantsクライアントを返す。認証情報未設定なら None を返す。"""
    global _client
    if _client is not None:
        return _client

    try:
        import jquantsapi

        if JQUANTS_REFRESH_TOKEN:
            cl = jquantsapi.Client(refresh_token=JQUANTS_REFRESH_TOKEN)
        elif JQUANTS_EMAIL and JQUANTS_PASSWORD:
            cl = jquantsapi.Client(mail_address=JQUANTS_EMAIL, password=JQUANTS_PASSWORD)
        else:
            return None

        _client = cl
        return cl
    except Exception:
        return None


def get_stock_prices(
    ticker: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """JQuants API で日本株の株価データを取得する。

    Args:
        ticker: 東証ティッカー。yfinance形式（例: '6758.T'）を受け付ける
        from_date: 開始日 'YYYY-MM-DD'。省略すると90日前
        to_date:   終了日 'YYYY-MM-DD'。省略すると昨日

    Returns:
        pandas DataFrame (open/high/low/close/volume) または None（失敗時）
    """
    cl = _get_client()
    if cl is None:
        return None

    # ティッカー変換: '6758.T' → '67580'
    code = ticker.replace(".T", "").replace(".OS", "")
    if len(code) == 4:
        code = code + "0"

    if from_date is None:
        from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    if to_date is None:
        to_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = cl.get_prices_daily_quotes(
            code=code,
            from_yyyymmdd=from_date.replace("-", ""),
            to_yyyymmdd=to_date.replace("-", ""),
        )
        if df is None or df.empty:
            return None

        df = df.rename(columns={
            "Date": "Date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()

        needed = ["open", "high", "low", "close", "volume"]
        available = [c for c in needed if c in df.columns]
        if len(available) < 4:
            return None

        return df[available].dropna()

    except Exception:
        return None


def is_available() -> bool:
    """JQuants APIが利用可能かどうかを返す。"""
    return _get_client() is not None

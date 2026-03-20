#!/usr/bin/env python3
"""
イベントフィルター — 重要経済イベント前後の新規エントリーを抑制する

使い方:
    from event_filter import EventFilter
    ef = EventFilter()
    if ef.should_block_entry("us"):
        print("イベント前のため新規エントリー禁止")

設計:
    - event_calendar.json から重要イベント日程を読み込む
    - イベント24時間前: 新規エントリー禁止
    - イベント12時間後: エントリー解禁
    - 既存ポジションのSL/TP監視は常に稼働（影響なし）
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional


class EventFilter:
    BLACKOUT_BEFORE_HOURS = 24
    RESUME_AFTER_HOURS = 12

    def __init__(self, calendar_path: Optional[str] = None):
        if calendar_path is None:
            calendar_path = os.path.join(os.path.dirname(__file__), "event_calendar.json")
        self._events = []
        self._load(calendar_path)

    def _load(self, path: str):
        try:
            with open(path) as f:
                data = json.load(f)
            for ev in data.get("events", []):
                self._events.append({
                    "date": datetime.strptime(ev["date"], "%Y-%m-%d"),
                    "name": ev["name"],
                    "impact": ev.get("impact", "high"),
                    "markets": ev.get("markets", []),
                })
        except (FileNotFoundError, json.JSONDecodeError):
            self._events = []

    def should_block_entry(self, market: str, now: Optional[datetime] = None) -> bool:
        """指定市場で新規エントリーを禁止すべきか判定"""
        if now is None:
            now = datetime.now()

        blocking = self.get_blocking_event(market, now)
        return blocking is not None

    def get_blocking_event(self, market: str, now: Optional[datetime] = None) -> Optional[dict]:
        """エントリーを阻止しているイベントを返す。なければNone"""
        if now is None:
            now = datetime.now()

        for ev in self._events:
            if ev["impact"] != "high":
                continue
            if market not in ev["markets"]:
                continue

            event_time = ev["date"].replace(hour=14, minute=0)  # イベント発表は14:00想定
            blackout_start = event_time - timedelta(hours=self.BLACKOUT_BEFORE_HOURS)
            resume_time = event_time + timedelta(hours=self.RESUME_AFTER_HOURS)

            if blackout_start <= now <= resume_time:
                return {
                    "name": ev["name"],
                    "date": ev["date"].strftime("%Y-%m-%d"),
                    "blackout_start": blackout_start.isoformat(),
                    "resume_time": resume_time.isoformat(),
                }

        return None

    def next_event(self, market: str, now: Optional[datetime] = None) -> Optional[dict]:
        """次に来る重要イベントを返す"""
        if now is None:
            now = datetime.now()

        future = [ev for ev in self._events
                  if ev["date"] >= now and market in ev["markets"] and ev["impact"] == "high"]
        if not future:
            return None

        nearest = min(future, key=lambda e: e["date"])
        days_until = (nearest["date"] - now).days
        return {
            "name": nearest["name"],
            "date": nearest["date"].strftime("%Y-%m-%d"),
            "days_until": days_until,
        }


if __name__ == "__main__":
    ef = EventFilter()
    now = datetime.now()
    print(f"現在時刻: {now.strftime('%Y-%m-%d %H:%M')}\n")

    for market in ["us", "jp", "btc", "fx", "gold"]:
        blocked = ef.should_block_entry(market, now)
        blocking = ef.get_blocking_event(market, now)
        nxt = ef.next_event(market, now)

        status = "BLOCKED" if blocked else "OK"
        print(f"[{market:5s}] {status}")
        if blocking:
            print(f"         阻止中: {blocking['name']} ({blocking['date']})")
            print(f"         解禁: {blocking['resume_time']}")
        if nxt:
            print(f"         次イベント: {nxt['name']} ({nxt['date']}, {nxt['days_until']}日後)")
        print()

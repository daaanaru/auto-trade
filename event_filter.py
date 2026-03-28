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
    MSQ_VOLATILITY_ALERT_HOURS = 24  # MSQ前日からボラ警戒

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
                    "type": ev.get("type", ""),
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

    def is_msq_volatility_window(self, market: str, now: Optional[datetime] = None) -> Optional[dict]:
        """MSQ（メジャーSQ/ミニSQ/トリプルウィッチング）前日のボラ警戒期間か判定。
        MSQ当日・前日はボラティリティが高くなりやすいため警戒フラグを返す。
        エントリーは禁止しないが、ポジションサイズ縮小の判断材料として使う。
        """
        if now is None:
            now = datetime.now()

        for ev in self._events:
            if ev.get("type", "") not in ("msq", "msq_mini"):
                continue
            if market not in ev["markets"]:
                continue

            event_time = ev["date"].replace(hour=15, minute=0)  # SQ算出は15:00
            alert_start = event_time - timedelta(hours=self.MSQ_VOLATILITY_ALERT_HOURS)

            if alert_start <= now <= event_time:
                return {
                    "name": ev["name"],
                    "date": ev["date"].strftime("%Y-%m-%d"),
                    "type": ev.get("type", "msq"),
                    "hours_until": max(0, (event_time - now).total_seconds() / 3600),
                }

        return None

    def is_market_holiday(self, market: str, now: Optional[datetime] = None) -> Optional[dict]:
        """指定日が市場休場日か判定"""
        if now is None:
            now = datetime.now()

        target_date = now.date() if hasattr(now, 'date') else now
        for ev in self._events:
            if ev.get("type", "") not in ("holiday_jp", "holiday_us"):
                continue
            if market not in ev["markets"]:
                continue
            if ev["date"].date() == target_date:
                return {
                    "name": ev["name"],
                    "date": ev["date"].strftime("%Y-%m-%d"),
                    "impact": ev["impact"],  # "holiday" or "half_day"
                }
        return None

    def get_upcoming_events(self, market: str, days: int = 7, now: Optional[datetime] = None) -> list:
        """今後N日間のイベント一覧を返す（全種類）"""
        if now is None:
            now = datetime.now()

        cutoff = now + timedelta(days=days)
        upcoming = []
        for ev in self._events:
            if market not in ev["markets"]:
                continue
            if now <= ev["date"] <= cutoff:
                upcoming.append({
                    "name": ev["name"],
                    "date": ev["date"].strftime("%Y-%m-%d"),
                    "impact": ev["impact"],
                    "type": ev.get("type", ""),
                    "days_until": (ev["date"] - now).days,
                })

        upcoming.sort(key=lambda e: e["date"])
        return upcoming


if __name__ == "__main__":
    ef = EventFilter()
    now = datetime.now()
    print(f"現在時刻: {now.strftime('%Y-%m-%d %H:%M')}\n")

    for market in ["us", "jp", "btc", "fx", "gold"]:
        blocked = ef.should_block_entry(market, now)
        blocking = ef.get_blocking_event(market, now)
        nxt = ef.next_event(market, now)
        msq = ef.is_msq_volatility_window(market, now)
        holiday = ef.is_market_holiday(market, now)

        status = "BLOCKED" if blocked else "OK"
        print(f"[{market:5s}] {status}")
        if blocking:
            print(f"         阻止中: {blocking['name']} ({blocking['date']})")
            print(f"         解禁: {blocking['resume_time']}")
        if msq:
            print(f"         MSQ警戒: {msq['name']} ({msq['date']}, あと{msq['hours_until']:.0f}時間)")
        if holiday:
            print(f"         休場: {holiday['name']} ({holiday['impact']})")
        if nxt:
            print(f"         次イベント: {nxt['name']} ({nxt['date']}, {nxt['days_until']}日後)")

        upcoming = ef.get_upcoming_events(market, days=7, now=now)
        if upcoming:
            print(f"         今後7日間: {len(upcoming)}件")
            for u in upcoming[:3]:
                print(f"           - {u['date']} {u['name']} ({u['impact']})")
        print()

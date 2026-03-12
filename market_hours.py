"""
市場営業時間管理モジュール

各市場の営業時間を管理し、「今この市場は開いているか」を判定する。
スクリーナーやペーパートレーダーが無駄なAPI呼び出しを避けるために使う。

対応市場:
  - jp: 東証（9:00-15:30 JST, 平日のみ）
  - us: NYSE/NASDAQ（9:30-16:00 ET, 平日のみ）
  - btc: 暗号資産（24時間365日）
  - gold: ゴールドETF（NYSE準拠）
  - fx: 外国為替（24時間, 平日のみ ≒ 月曜7:00 JST〜土曜7:00 JST）
"""

from datetime import datetime, time, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")


def _now_jst() -> datetime:
    return datetime.now(JST)


def _now_et() -> datetime:
    return datetime.now(ET)


def is_market_open(market_key: str) -> bool:
    """市場が現在開場中かを判定する。

    Args:
        market_key: "jp", "us", "btc", "gold", "fx"

    Returns:
        True = 開場中（データ取得する意味がある）
        False = 閉場中（スキップすべき）
    """
    now_jst = _now_jst()
    weekday = now_jst.weekday()  # 0=月, 6=日

    # --- 暗号資産: 24時間365日 ---
    if market_key == "btc":
        return True

    # --- FX: 24時間だが土日は閉場 ---
    # 実際は日曜17:00 ET（月曜7:00 JST頃）にオープン、
    # 金曜17:00 ET（土曜7:00 JST頃）にクローズ
    if market_key == "fx":
        if weekday == 5:  # 土曜
            return now_jst.time() < time(7, 0)  # 土曜7:00 JSTまで
        if weekday == 6:  # 日曜
            return False
        if weekday == 0:  # 月曜
            return now_jst.time() >= time(7, 0) or True  # 月曜は基本開場
        return True  # 火〜金は24時間

    # --- 土日は株式市場は閉場 ---
    if weekday >= 5:
        return False

    # --- 東証: 9:00-15:30 JST ---
    if market_key == "jp":
        t = now_jst.time()
        return time(9, 0) <= t <= time(15, 30)

    # --- NYSE/NASDAQ, ゴールドETF: 9:30-16:00 ET ---
    if market_key in ("us", "gold"):
        now_et = _now_et()
        # ETでも平日チェック（JST土曜朝 = ET金曜夜の可能性）
        if now_et.weekday() >= 5:
            return False
        t_et = now_et.time()
        return time(9, 30) <= t_et <= time(16, 0)

    return False


def is_market_recently_closed(market_key: str, within_hours: int = 2) -> bool:
    """市場が最近（within_hours時間以内に）閉場したかを判定する。

    閉場直後はデータが確定しているため、1日1回のシグナル更新に使える。
    """
    now_jst = _now_jst()
    weekday = now_jst.weekday()

    if market_key in ("btc", "fx"):
        return False  # 常時開場系は「最近閉場した」はない

    if weekday >= 5:
        return False

    if market_key == "jp":
        t = now_jst.time()
        close_time = time(15, 30)
        if t > close_time:
            minutes_since_close = (
                (t.hour * 60 + t.minute) - (close_time.hour * 60 + close_time.minute)
            )
            return minutes_since_close <= within_hours * 60
        return False

    if market_key in ("us", "gold"):
        now_et = _now_et()
        if now_et.weekday() >= 5:
            return False
        t_et = now_et.time()
        close_time = time(16, 0)
        if t_et > close_time:
            minutes_since_close = (
                (t_et.hour * 60 + t_et.minute) - (close_time.hour * 60 + close_time.minute)
            )
            return minutes_since_close <= within_hours * 60
        return False

    return False


def should_scan(market_key: str) -> bool:
    """この市場を今スキャンすべきかを判定する。

    開場中 or 閉場直後（2時間以内）ならスキャンする。
    """
    return is_market_open(market_key) or is_market_recently_closed(market_key)


def get_optimal_interval(market_key: str) -> str:
    """市場の状態に応じて最適なデータ取得間隔を返す。

    開場中: 短期足（15分足）で最新価格を追跡
    閉場中: 日足で十分
    """
    if is_market_open(market_key):
        return "15m"
    return "1d"


def get_optimal_period(market_key: str) -> str:
    """intervalに応じた適切なperiodを返す。

    15m足: period="5d"（yfinance制限）
    1d足: period="3mo"
    """
    if is_market_open(market_key):
        return "5d"
    return "3mo"


def get_market_status_summary() -> dict:
    """全市場の開場状況をまとめて返す。"""
    markets = ["jp", "us", "btc", "gold", "fx"]
    return {
        m: {
            "open": is_market_open(m),
            "should_scan": should_scan(m),
        }
        for m in markets
    }


if __name__ == "__main__":
    print("=== 市場営業時間チェック ===")
    now = _now_jst()
    print(f"現在時刻: {now.strftime('%Y-%m-%d %H:%M JST')} ({['月','火','水','木','金','土','日'][now.weekday()]}曜)")
    print()
    for market, status in get_market_status_summary().items():
        icon = "🟢" if status["open"] else "🔴"
        scan = "スキャン対象" if status["should_scan"] else "スキップ"
        print(f"  {icon} {market:>5}: {'開場' if status['open'] else '閉場'} → {scan}")

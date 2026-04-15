#!/usr/bin/env python3
"""
turtle_live.py — 55/20 Turtle System 2 本番運用スクリプト

毎日:
  1. 各銘柄の 1d バーを取得
  2. 現在のポジション (state) と突き合わせ
  3. 新規エントリー判定 (55バー Donchian ブレイクアウト)
  4. エグジット判定 (10バー反対側 / 2 ATR SL / 3 ATR TP)
  5. 推奨アクションを Discord 通知
  6. DRY_RUN の場合は約定せず、ログのみ
  7. LIVE の場合は手動発注（Stage 1-2）or API発注（Stage 3以降、将来実装）

依拠:
  - mtt_4h_trend.py (バックテストで validate 済)
  - /tmp/mtt_turtle_multi_asset.py (Crypto+Gold で WF 4/4 合格)
  - LIVE_TRADE_DESIGN.md §21 (統合設計)

CLI:
  python3 turtle_live.py --status                    # 現状表示
  python3 turtle_live.py --check                     # 日次判定+通知
  python3 turtle_live.py --check --dry-run           # 強制DRY_RUN
  python3 turtle_live.py --record-entry SYM PRICE    # 約定記録(手動)
  python3 turtle_live.py --record-exit SYM PRICE     # 決済記録(手動)
  python3 turtle_live.py --reset                     # state リセット
"""

import argparse
import json
import os
import sys
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest_live_design import fetch_ccxt_long, calc_atr
from engine import YFinanceFetcher

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ==============================================================
# 設定
# ==============================================================

# Crypto (via Bybit CCXT) — SOL 除外 (唯一の負け銘柄)
CRYPTO_UNIVERSE = [
    {"code": "BTC/USDT", "name": "Bitcoin",  "source": "ccxt"},
    {"code": "ETH/USDT", "name": "Ethereum", "source": "ccxt"},
    {"code": "XRP/USDT", "name": "XRP",      "source": "ccxt"},
    {"code": "BNB/USDT", "name": "BNB",      "source": "ccxt"},
    {"code": "ADA/USDT", "name": "Cardano",  "source": "ccxt"},
    {"code": "DOGE/USDT", "name": "Dogecoin", "source": "ccxt"},
    {"code": "AVAX/USDT", "name": "Avalanche","source": "ccxt"},
]

# Gold (via yfinance) — GLD ETF と GC=F 先物
GOLD_UNIVERSE = [
    {"code": "GLD", "name": "SPDR Gold ETF", "source": "yfinance"},
    # GC=F は先物で実弾化ハードル高いため default 非活性化
    # {"code": "GC=F", "name": "Gold Futures", "source": "yfinance"},
]

UNIVERSE = CRYPTO_UNIVERSE + GOLD_UNIVERSE

# Turtle System 2 パラメータ (55/20)
ENTRY_LOOKBACK = 55
EXIT_LOOKBACK = 20
ATR_PERIOD = 14
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.0
MIN_ATR_PCT = 0.002

# ファイル
STATE_FILE = PROJECT_ROOT / "turtle_live_state.json"
LOG_FILE = PROJECT_ROOT / "turtle_live_log.json"


# ==============================================================
# State 管理
# ==============================================================

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "created_at": datetime.now().isoformat(),
        "last_check_date": None,
        "stage": 0,       # 0=DRY_RUN, 1=micro(¥100K), 2=small(¥500K), 3=full(¥1.5M)
        "dry_run": True,
        "allocated_capital_jpy": 0,
        "positions": {},  # code -> {entry_price, entry_date, entry_atr, size, side}
        "total_checks": 0,
        "total_entries": 0,
        "total_exits": 0,
        "realized_pnl_jpy": 0.0,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def append_log(entry):
    log = []
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, "r") as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []
    log.append(entry)
    if len(log) > 1000:
        log = log[-1000:]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# データ取得
# ==============================================================

def fetch_bars(asset: dict, days: int = 200) -> pd.DataFrame:
    """単一資産の 1d バーを取得"""
    code = asset["code"]
    source = asset["source"]
    if source == "ccxt":
        return fetch_ccxt_long("bybit", code, "1d", days)
    else:  # yfinance
        fetcher = YFinanceFetcher()
        period = "2y" if days >= 500 else "1y"
        return fetcher.fetch(code, period=period, interval="1d")


# ==============================================================
# シグナル判定
# ==============================================================

def evaluate_asset(asset: dict, state: dict) -> dict:
    """
    1資産について判定:
      - 現在保有中か
      - 保有中ならエグジットシグナルがあるか
      - 保有中でなければエントリーシグナルがあるか
    """
    code = asset["code"]
    result = {
        "code": code,
        "name": asset["name"],
        "source": asset["source"],
        "action": "HOLD",   # HOLD / ENTRY_LONG / ENTRY_SHORT / EXIT
        "reason": "",
        "current_price": None,
        "atr": None,
    }

    try:
        df = fetch_bars(asset, days=200)
    except Exception as e:
        result["action"] = "ERROR"
        result["reason"] = f"データ取得失敗: {e}"
        return result

    if df is None or len(df) < ENTRY_LOOKBACK + 5:
        result["action"] = "ERROR"
        result["reason"] = f"データ不足 ({len(df) if df is not None else 0}バー)"
        return result

    # ATR
    atr = calc_atr(df, period=ATR_PERIOD)
    current_atr = float(atr.iloc[-1])
    current_close = float(df["close"].iloc[-1])
    current_high = float(df["high"].iloc[-1])
    current_low = float(df["low"].iloc[-1])
    latest_date = df.index[-1].strftime("%Y-%m-%d")

    result["current_price"] = round(current_close, 4)
    result["atr"] = round(current_atr, 4)
    result["latest_bar_date"] = latest_date

    if np.isnan(current_atr) or current_atr <= 0:
        result["action"] = "ERROR"
        result["reason"] = "ATR 不正"
        return result

    # 前55バー/20バーの極値 (現在バーを除く)
    lookback_high_55 = float(df["high"].iloc[-ENTRY_LOOKBACK - 1:-1].max())
    lookback_low_55 = float(df["low"].iloc[-ENTRY_LOOKBACK - 1:-1].min())
    lookback_high_20 = float(df["high"].iloc[-EXIT_LOOKBACK - 1:-1].max())
    lookback_low_20 = float(df["low"].iloc[-EXIT_LOOKBACK - 1:-1].min())

    result["donchian_55_high"] = round(lookback_high_55, 4)
    result["donchian_55_low"] = round(lookback_low_55, 4)
    result["donchian_20_high"] = round(lookback_high_20, 4)
    result["donchian_20_low"] = round(lookback_low_20, 4)

    # 現在のポジション
    position = state["positions"].get(code)

    if position:
        # 保有中 → エグジット判定
        side = position["side"]
        entry_price = position["entry_price"]
        entry_atr = position["entry_atr"]
        sl_dist = entry_atr * SL_ATR_MULT
        tp_dist = entry_atr * TP_ATR_MULT

        result["current_position"] = {
            "side": side,
            "entry_price": entry_price,
            "entry_date": position["entry_date"],
            "entry_atr": entry_atr,
            "unrealized_pnl_per_unit": round(
                (current_close - entry_price) if side == "LONG" else (entry_price - current_close),
                4,
            ),
        }

        exit_triggered = False
        exit_reason = ""
        exit_level = None

        if side == "LONG":
            if current_low <= entry_price - sl_dist:
                exit_triggered = True
                exit_reason = "STOP_LOSS"
                exit_level = entry_price - sl_dist
            elif current_high >= entry_price + tp_dist:
                exit_triggered = True
                exit_reason = "TAKE_PROFIT"
                exit_level = entry_price + tp_dist
            elif current_low <= lookback_low_20:
                exit_triggered = True
                exit_reason = "TURTLE_EXIT"
                exit_level = lookback_low_20
        else:  # SHORT
            if current_high >= entry_price + sl_dist:
                exit_triggered = True
                exit_reason = "STOP_LOSS"
                exit_level = entry_price + sl_dist
            elif current_low <= entry_price - tp_dist:
                exit_triggered = True
                exit_reason = "TAKE_PROFIT"
                exit_level = entry_price - tp_dist
            elif current_high >= lookback_high_20:
                exit_triggered = True
                exit_reason = "TURTLE_EXIT"
                exit_level = lookback_high_20

        if exit_triggered:
            result["action"] = "EXIT"
            result["reason"] = exit_reason
            result["exit_level"] = round(exit_level, 4)
        else:
            result["action"] = "HOLD"
            result["reason"] = f"継続保有 ({side})"
    else:
        # 未保有 → エントリー判定
        atr_pct = current_atr / current_close
        if atr_pct < MIN_ATR_PCT:
            result["action"] = "HOLD"
            result["reason"] = f"ボラ不足 ATR%={atr_pct*100:.2f}%"
            return result

        if current_high >= lookback_high_55:
            result["action"] = "ENTRY_LONG"
            result["reason"] = f"55バー高値ブレイク (現在High {current_high:.2f} >= {lookback_high_55:.2f})"
            result["suggested_sl"] = round(current_close - current_atr * SL_ATR_MULT, 4)
            result["suggested_tp"] = round(current_close + current_atr * TP_ATR_MULT, 4)
        elif current_low <= lookback_low_55:
            result["action"] = "ENTRY_SHORT"
            result["reason"] = f"55バー安値ブレイク (現在Low {current_low:.2f} <= {lookback_low_55:.2f})"
            result["suggested_sl"] = round(current_close + current_atr * SL_ATR_MULT, 4)
            result["suggested_tp"] = round(current_close - current_atr * TP_ATR_MULT, 4)
        else:
            result["action"] = "HOLD"
            result["reason"] = "シグナルなし"

    return result


# ==============================================================
# Discord 通知
# ==============================================================

def send_discord(message: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("TURTLE_DISCORD_WEBHOOK")
    if not webhook:
        print("  ⚠️ Discord Webhook 未設定")
        return False
    try:
        import urllib.request
        data = json.dumps({"content": message[:1900]}).encode("utf-8")
        req = urllib.request.Request(webhook, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ⚠️ Discord通知失敗: {e}")
        return False


def format_report(results: list, state: dict) -> str:
    mode = "DRY_RUN" if state.get("dry_run", True) else f"Stage {state['stage']} LIVE"
    lines = [
        f"🐢 **Turtle System 2 日次判定 [{mode}]**",
        f"日付: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # アクションを種類別に集計
    entries = [r for r in results if r["action"] in ("ENTRY_LONG", "ENTRY_SHORT")]
    exits = [r for r in results if r["action"] == "EXIT"]
    holds = [r for r in results if r["action"] == "HOLD"]
    errors = [r for r in results if r["action"] == "ERROR"]

    if entries:
        lines.append(f"🟢 **新規エントリー推奨 ({len(entries)}件)**")
        for r in entries:
            side = "LONG" if r["action"] == "ENTRY_LONG" else "SHORT"
            lines.append(f"  ▸ `{r['code']}` **{side}** @ ${r['current_price']}")
            lines.append(f"    SL: ${r.get('suggested_sl', '-')} / TP: ${r.get('suggested_tp', '-')}")
            lines.append(f"    理由: {r['reason']}")
        lines.append("")

    if exits:
        lines.append(f"🔴 **決済推奨 ({len(exits)}件)**")
        for r in exits:
            pos = r.get("current_position", {})
            lines.append(f"  ▸ `{r['code']}` **EXIT** @ ${r['current_price']}")
            lines.append(f"    エントリー: ${pos.get('entry_price')} ({pos.get('side')})")
            lines.append(f"    含み損益/unit: {pos.get('unrealized_pnl_per_unit'):+.4f}")
            lines.append(f"    理由: {r['reason']}")
        lines.append("")

    if holds or errors:
        lines.append(f"⚪ **継続保有/待機 ({len(holds)+len(errors)}件)**")
        for r in holds + errors:
            status_icon = "✓" if r["action"] == "HOLD" else "❌"
            lines.append(f"  {status_icon} `{r['code']}`: {r['reason']}")
        lines.append("")

    # 現在保有状況
    positions = state.get("positions", {})
    if positions:
        lines.append(f"📊 **現在保有 ({len(positions)}件)**")
        for code, pos in positions.items():
            lines.append(f"  • `{code}` {pos['side']} @ ${pos['entry_price']} "
                        f"(開始 {pos['entry_date'][:10]})")

    if mode.startswith("DRY"):
        lines.append("")
        lines.append("⚠️ **DRY_RUN**: 実発注は行われません。Stage 1+ で上様の承認＋手動発注をお願いします。")

    return "\n".join(lines)


# ==============================================================
# コマンド
# ==============================================================

def action_status(state: dict):
    print("\n" + "=" * 70)
    print("  Turtle System 2 現在状況")
    print("=" * 70)
    print(f"  Stage         : {state['stage']} ({'DRY_RUN' if state['dry_run'] else 'LIVE'})")
    print(f"  配分資本      : ¥{state.get('allocated_capital_jpy', 0):,.0f}")
    print(f"  確定損益      : ¥{state.get('realized_pnl_jpy', 0):+,.0f}")
    print(f"  判定回数      : {state.get('total_checks', 0)}")
    print(f"  総エントリー数: {state.get('total_entries', 0)}")
    print(f"  総決済数      : {state.get('total_exits', 0)}")
    print(f"  最終判定日    : {state.get('last_check_date', '(未実行)')}")
    print()
    positions = state.get("positions", {})
    if positions:
        print(f"  現在保有 ({len(positions)}件):")
        for code, pos in positions.items():
            print(f"    {code:<15} {pos['side']:<5} @ ${pos['entry_price']:>10.4f} "
                  f"since {pos['entry_date'][:10]}")
    else:
        print("  現在保有: なし")
    print("=" * 70)


def action_check(state: dict, dry_run: bool):
    print(f"\n[Turtle Live] 日次判定開始 ({'DRY_RUN' if dry_run else 'LIVE'})")
    print(f"  対象: {len(UNIVERSE)}資産 (Crypto {len(CRYPTO_UNIVERSE)} + Gold {len(GOLD_UNIVERSE)})")

    results = []
    for asset in UNIVERSE:
        print(f"\n  [{asset['code']}] {asset['name']} ({asset['source']})")
        result = evaluate_asset(asset, state)
        action = result["action"]
        reason = result["reason"]
        price = result.get("current_price", "-")
        print(f"    → {action}: {reason} (price: ${price})")
        if action in ("ENTRY_LONG", "ENTRY_SHORT"):
            print(f"       SL: ${result.get('suggested_sl')} / TP: ${result.get('suggested_tp')}")
        elif action == "EXIT":
            print(f"       exit_level: ${result.get('exit_level')}")
        results.append(result)

    # Discord 通知
    print("\n  Discord 通知送信...")
    message = format_report(results, state)
    sent = send_discord(message)
    print(f"    → {'成功' if sent else '失敗 (ログのみ)'}")

    # State 更新
    state["last_check_date"] = datetime.now().strftime("%Y-%m-%d")
    state["total_checks"] = state.get("total_checks", 0) + 1
    save_state(state)

    # ログ記録
    append_log({
        "timestamp": datetime.now().isoformat(),
        "stage": state["stage"],
        "dry_run": dry_run,
        "results": results,
        "discord_sent": sent,
    })

    print(f"\n✅ 日次判定完了 (検証 {len(results)}資産)")

    # サマリー
    n_entries = sum(1 for r in results if r["action"] in ("ENTRY_LONG", "ENTRY_SHORT"))
    n_exits = sum(1 for r in results if r["action"] == "EXIT")
    n_holds = sum(1 for r in results if r["action"] == "HOLD")
    n_errors = sum(1 for r in results if r["action"] == "ERROR")
    print(f"  エントリー推奨: {n_entries} / 決済推奨: {n_exits} / 継続保有: {n_holds} / エラー: {n_errors}")


def action_record_entry(state: dict, code: str, price: float, side: str = "LONG"):
    """上様が手動発注後、約定価格を記録"""
    # ATR を取得
    asset = next((a for a in UNIVERSE if a["code"] == code), None)
    if asset is None:
        print(f"  ❌ 未知のコード: {code}")
        return
    df = fetch_bars(asset, days=100)
    atr_series = calc_atr(df, period=ATR_PERIOD)
    current_atr = float(atr_series.iloc[-1])

    state["positions"][code] = {
        "side": side,
        "entry_price": float(price),
        "entry_atr": current_atr,
        "entry_date": datetime.now().isoformat(),
    }
    state["total_entries"] = state.get("total_entries", 0) + 1
    save_state(state)
    print(f"  ✅ 記録完了: {code} {side} @ ${price} (ATR {current_atr:.4f})")


def action_record_exit(state: dict, code: str, price: float):
    """上様が手動決済後、約定価格を記録し PnL を計算"""
    position = state["positions"].get(code)
    if not position:
        print(f"  ❌ {code} は未保有")
        return
    direction = 1 if position["side"] == "LONG" else -1
    pnl_per_unit = direction * (float(price) - position["entry_price"])
    print(f"  決済: {code} {position['side']} @ ${price}")
    print(f"  エントリー ${position['entry_price']} → Exit ${price}")
    print(f"  PnL/unit: {pnl_per_unit:+.4f}")

    state["positions"].pop(code, None)
    state["total_exits"] = state.get("total_exits", 0) + 1
    # realized_pnl_jpy は上様が size と為替レートで手動入力 (将来拡張)
    save_state(state)
    print(f"  ✅ 記録完了")


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(description="Turtle System 2 日次運用")
    parser.add_argument("--status", action="store_true", help="現状表示")
    parser.add_argument("--check", action="store_true", help="日次判定+通知")
    parser.add_argument("--dry-run", action="store_true", help="強制DRY_RUN")
    parser.add_argument("--reset", action="store_true", help="state リセット")
    parser.add_argument("--record-entry", nargs=3, metavar=("CODE", "PRICE", "SIDE"),
                        help="手動エントリー記録 (例: --record-entry BTC/USDT 70000 LONG)")
    parser.add_argument("--record-exit", nargs=2, metavar=("CODE", "PRICE"),
                        help="手動決済記録 (例: --record-exit BTC/USDT 72000)")
    args = parser.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("  state リセット完了")

    state = load_state()
    if args.dry_run:
        state["dry_run"] = True

    if args.record_entry:
        code, price, side = args.record_entry
        action_record_entry(state, code, float(price), side)
        return

    if args.record_exit:
        code, price = args.record_exit
        action_record_exit(state, code, float(price))
        return

    if args.check:
        action_check(state, dry_run=state.get("dry_run", True))
        return

    # Default: status
    action_status(state)


if __name__ == "__main__":
    main()

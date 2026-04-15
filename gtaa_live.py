#!/usr/bin/env python3
"""
gtaa_live.py — GTAA 本番運用スクリプト

毎月末(または月初)に:
  1. 各資産の10ヶ月SMAを計算
  2. 価格 > SMA の資産を選別
  3. 現在保有との差分を「推奨リバランス」として出力
  4. Discord に通知
  5. ログ記録

Stage制御:
  - DRY_RUN=true (default): 計算と通知のみ、実際の発注なし
  - DRY_RUN=false: 実発注 (将来実装。現在は手動発注前提)

CLI:
  python3 gtaa_live.py --status          # 現在保有と次回判定予定を表示
  python3 gtaa_live.py --rebalance       # 判定実行+通知(月次)
  python3 gtaa_live.py --rebalance --dry-run   # 強制DRY_RUN
  python3 gtaa_live.py --rebalance --execute   # 実発注 (手動承認後、将来実装)

依拠:
  - gtaa_poc.py (新3原則 Double合格)
  - LIVE_TRADE_DESIGN.md §20
  - Meb Faber SSRN 962461
"""

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine import YFinanceFetcher

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ==============================================================
# 設定
# ==============================================================

GTAA_5_UNIVERSE = [
    ("SPY", "US Large Cap (S&P 500)"),
    ("EFA", "Developed ex-US"),
    ("IEF", "US 7-10y Treasury"),
    ("GLD", "Gold"),
    ("VNQ", "US REITs"),
]

SMA_MONTHS = 10
COMMISSION_RATE = 0.001    # 0.1% (仮定・実証券口座の手数料に合わせて調整)
SLIPPAGE_RATE = 0.0005

STATE_FILE = PROJECT_ROOT / "gtaa_live_state.json"
LOG_FILE = PROJECT_ROOT / "gtaa_live_log.json"


# ==============================================================
# State管理
# ==============================================================

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "created_at": datetime.now().isoformat(),
        "last_judgment_date": None,
        "last_rebalance_date": None,
        "holdings": {code: 0.0 for code, _ in GTAA_5_UNIVERSE},  # 数量 (shares)
        "initial_capital_jpy": 0.0,
        "current_capital_jpy": 0.0,  # 参考値
        "stage": 0,   # 0=DRY_RUN, 1=micro, 2=small, 3=full
        "dry_run": True,
        "total_judgments": 0,
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
    if len(log) > 500:
        log = log[-500:]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


# ==============================================================
# シグナル判定
# ==============================================================

def compute_signal() -> dict:
    """
    各資産について:
      - 最新月末価格
      - 10ヶ月SMA
      - 価格 > SMA (保有シグナル) or 価格 <= SMA (現金シグナル)
      - 推奨ウェイト (選別資産で等分)
    """
    fetcher = YFinanceFetcher()
    result = {
        "judgment_date": datetime.now().strftime("%Y-%m-%d"),
        "universe_size": len(GTAA_5_UNIVERSE),
        "assets": {},
        "selected": [],
        "cash_ratio": 0.0,
    }

    monthly_prices = {}
    for code, name in GTAA_5_UNIVERSE:
        try:
            # 2年分取れば10ヶ月SMAは十分
            df = fetcher.fetch(code, period="3y", interval="1d")
            if df is None or len(df) < 250:
                print(f"  ⚠️ {code}: データ不足")
                continue
            monthly = df["close"].resample("M").last().dropna()
            if len(monthly) < SMA_MONTHS + 1:
                print(f"  ⚠️ {code}: 月次データ不足 ({len(monthly)}<{SMA_MONTHS+1})")
                continue
            monthly_prices[code] = monthly

            latest_price = float(monthly.iloc[-1])
            sma = float(monthly.iloc[-SMA_MONTHS:].mean())
            latest_date = monthly.index[-1].strftime("%Y-%m-%d")
            above_sma = latest_price > sma
            distance_pct = (latest_price / sma - 1) * 100

            result["assets"][code] = {
                "name": name,
                "latest_date": latest_date,
                "latest_price_usd": round(latest_price, 4),
                "sma_10m": round(sma, 4),
                "distance_from_sma_pct": round(distance_pct, 2),
                "hold": above_sma,
            }
            if above_sma:
                result["selected"].append(code)
        except Exception as e:
            print(f"  ✗ {code}: {e}")
            result["assets"][code] = {"name": name, "error": str(e), "hold": False}

    # ウェイト計算
    n_selected = len(result["selected"])
    if n_selected > 0:
        weight = 1.0 / n_selected
        result["target_weights"] = {code: round(weight, 4) for code in result["selected"]}
        result["cash_ratio"] = 0.0
    else:
        result["target_weights"] = {}
        result["cash_ratio"] = 1.0

    # 現金ウェイト
    for code, _ in GTAA_5_UNIVERSE:
        if code not in result["target_weights"]:
            result["target_weights"][code] = 0.0

    result["cash_weight"] = 1.0 - sum(result["target_weights"].values())
    return result


# ==============================================================
# リバランス計算 (現在保有との差分)
# ==============================================================

def compute_rebalance(state: dict, signal: dict) -> dict:
    """
    現在保有(state["holdings"])と target_weights から差分を計算。
    Stage 0 (DRY_RUN) では「仮想の 10,000円」を使って計算。
    Stage 1以上では state["current_capital_jpy"] を使う。
    """
    initial = state.get("initial_capital_jpy", 0)
    if initial <= 0:
        initial = 10000.0  # DRY_RUNのデフォルト

    current_capital = state.get("current_capital_jpy") or initial

    # 現在ポジションの時価評価 (簡易版: state保存時点の value を使う)
    holdings_value = state.get("holdings_value", {code: 0.0 for code, _ in GTAA_5_UNIVERSE})
    cash_value = state.get("cash_value", current_capital)
    total_value = cash_value + sum(holdings_value.values())

    # 目標ウェイトから目標金額を計算
    target_alloc = {}
    for code in [c for c, _ in GTAA_5_UNIVERSE]:
        weight = signal["target_weights"].get(code, 0.0)
        target_alloc[code] = total_value * weight

    # 差分 (買い=正, 売り=負)
    actions = []
    for code, _ in GTAA_5_UNIVERSE:
        current = holdings_value.get(code, 0.0)
        target = target_alloc[code]
        delta = target - current
        action = "BUY" if delta > 0 else ("SELL" if delta < 0 else "HOLD")
        actions.append({
            "code": code,
            "action": action,
            "current_value": round(current, 2),
            "target_value": round(target, 2),
            "delta": round(delta, 2),
            "target_weight_pct": round(signal["target_weights"].get(code, 0) * 100, 2),
        })

    # 現金
    target_cash = total_value * signal["cash_weight"]

    return {
        "total_value": round(total_value, 2),
        "actions": actions,
        "target_cash": round(target_cash, 2),
        "current_cash": round(cash_value, 2),
    }


# ==============================================================
# Discord 通知
# ==============================================================

def send_discord(message: str, webhook_url: str = None):
    """Discord Webhookに通知を送る。失敗してもクラッシュしない。"""
    if webhook_url is None:
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("GTAA_DISCORD_WEBHOOK")
    if not webhook_url:
        print("  ⚠️ Discord Webhook未設定 (DISCORD_WEBHOOK_URL)")
        return False

    try:
        import urllib.request
        import urllib.error
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ⚠️ Discord通知失敗: {e}")
        return False


def format_notification(signal: dict, rebalance: dict, stage: int, dry_run: bool) -> str:
    """判定結果とリバランス推奨を Discord 向けに整形"""
    mode = "DRY_RUN" if dry_run else f"Stage {stage} LIVE"
    lines = [
        f"🎯 **GTAA 月次判定 [{mode}]**",
        f"判定日: {signal['judgment_date']}",
        f"保有候補: **{len(signal['selected'])}/{signal['universe_size']}資産**",
        "",
        "**各資産の状態**:",
    ]
    for code, info in signal["assets"].items():
        if "error" in info:
            lines.append(f"  ❌ `{code}` エラー: {info['error']}")
            continue
        mark = "🟢 HOLD" if info["hold"] else "⚪ CASH"
        lines.append(
            f"  {mark} `{code}` ({info['name']}): "
            f"${info['latest_price_usd']:.2f} vs SMA10 ${info['sma_10m']:.2f} "
            f"({info['distance_from_sma_pct']:+.2f}%)"
        )

    if signal["selected"]:
        lines.append("")
        lines.append(f"**推奨配分 (等ウェイト)**: 1資産あたり {100/len(signal['selected']):.1f}%")
    else:
        lines.append("")
        lines.append("**推奨配分**: 全額現金（全資産がSMA下回り）")

    lines.append("")
    lines.append(f"**総評価額**: ¥{rebalance['total_value']:,.0f}")
    lines.append(f"**目標現金**: ¥{rebalance['target_cash']:,.0f} (現在 ¥{rebalance['current_cash']:,.0f})")

    # 差分アクション
    nontrivial_actions = [a for a in rebalance["actions"] if abs(a["delta"]) >= 0.01]
    if nontrivial_actions:
        lines.append("")
        lines.append("**推奨アクション**:")
        for a in nontrivial_actions:
            lines.append(
                f"  {'🟢 BUY' if a['action']=='BUY' else '🔴 SELL'} "
                f"`{a['code']}`: ¥{a['current_value']:,.0f} → ¥{a['target_value']:,.0f} "
                f"(差分 ¥{a['delta']:+,.0f}, 目標比率 {a['target_weight_pct']:.1f}%)"
            )

    if dry_run:
        lines.append("")
        lines.append("⚠️ **DRY_RUN**: 実発注は行われません。上様の承認と手動発注をお願いいたします。")

    return "\n".join(lines)


# ==============================================================
# メイン処理
# ==============================================================

def action_status(state: dict):
    print("\n" + "=" * 70)
    print("  GTAA 現在状況")
    print("=" * 70)
    print(f"  Stage          : {state['stage']} ({'DRY_RUN' if state['dry_run'] else 'LIVE'})")
    print(f"  初期資金       : ¥{state.get('initial_capital_jpy', 0):,.0f}")
    print(f"  現在評価額     : ¥{state.get('current_capital_jpy', 0):,.0f}")
    print(f"  最終判定日     : {state.get('last_judgment_date', '(未実行)')}")
    print(f"  最終リバランス : {state.get('last_rebalance_date', '(未実行)')}")
    print(f"  判定回数       : {state.get('total_judgments', 0)}")
    print("\n  現在保有:")
    holdings_value = state.get("holdings_value", {})
    for code, name in GTAA_5_UNIVERSE:
        val = holdings_value.get(code, 0)
        print(f"    {code} ({name}): ¥{val:,.0f}")
    print(f"    現金: ¥{state.get('cash_value', 0):,.0f}")


def action_rebalance(state: dict, execute: bool = False, dry_run: bool = True):
    """月次判定を実行"""
    print("\n[GTAA Live] 月次判定開始")

    # シグナル計算
    print("[1/3] 各資産のシグナル計算")
    signal = compute_signal()
    print(f"  保有候補: {len(signal['selected'])}/{signal['universe_size']}")
    for code in [c for c, _ in GTAA_5_UNIVERSE]:
        info = signal["assets"].get(code, {})
        if "error" in info:
            print(f"    ❌ {code}: {info['error']}")
            continue
        mark = "🟢 HOLD" if info.get("hold") else "⚪ CASH"
        print(f"    {mark} {code}: "
              f"${info.get('latest_price_usd', 0):.2f} vs SMA10 ${info.get('sma_10m', 0):.2f} "
              f"({info.get('distance_from_sma_pct', 0):+.2f}%)")

    # リバランス計算
    print("\n[2/3] リバランス差分計算")
    rebalance = compute_rebalance(state, signal)
    print(f"  総評価額: ¥{rebalance['total_value']:,.0f}")
    nontrivial = [a for a in rebalance["actions"] if abs(a["delta"]) >= 0.01]
    if nontrivial:
        for a in nontrivial:
            print(f"    {a['action']:4} {a['code']}: "
                  f"¥{a['current_value']:,.0f} → ¥{a['target_value']:,.0f} "
                  f"(差分 ¥{a['delta']:+,.0f})")
    else:
        print("  リバランス不要 (現在配分と一致)")

    # 通知
    print("\n[3/3] Discord 通知")
    message = format_notification(signal, rebalance, state["stage"], dry_run)
    sent = send_discord(message)
    print(f"  Discord送信: {'成功' if sent else '失敗 (ログのみ)'}")

    # ログとState更新
    state["last_judgment_date"] = signal["judgment_date"]
    state["total_judgments"] = state.get("total_judgments", 0) + 1

    if execute and not dry_run:
        # 実発注 (Stage 3以降用、現在は未実装)
        print("\n  ⚠️ 実発注モードは未実装。Stage 1-2 では手動発注を上様にお願いします")
        # 手動発注後、上様が --record-fill で state を更新する設計

    save_state(state)

    # ログ記録
    append_log({
        "timestamp": datetime.now().isoformat(),
        "stage": state["stage"],
        "dry_run": dry_run,
        "signal": signal,
        "rebalance": rebalance,
        "discord_sent": sent,
    })

    print("\n✅ 月次判定完了")
    return signal, rebalance


def main():
    parser = argparse.ArgumentParser(description="GTAA 月次運用スクリプト")
    parser.add_argument("--status", action="store_true", help="現在状況を表示")
    parser.add_argument("--rebalance", action="store_true", help="月次判定を実行し通知")
    parser.add_argument("--dry-run", action="store_true", help="強制DRY_RUN")
    parser.add_argument("--execute", action="store_true",
                        help="実発注モード (Stage 3以降、現在未実装)")
    parser.add_argument("--reset", action="store_true", help="state をリセット")
    args = parser.parse_args()

    # State読み込み
    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("  state リセット完了")

    state = load_state()

    # DRY_RUN フラグ制御
    if args.dry_run:
        state["dry_run"] = True
    dry_run = state.get("dry_run", True)

    if args.status or (not args.rebalance and not args.reset):
        action_status(state)
        return

    if args.rebalance:
        action_rebalance(state, execute=args.execute, dry_run=dry_run)
        return


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
investment_reviewer.py -- 投資部門 日次評価・戦略レビュー自動生成

毎日の投資成績を自動で評価・分析し、改善提言を生成する。
heartbeatの投資ローテーションからCLIで呼び出す想定。

機能:
  1. 日次サマリー（ポジション数、決済件数、実現損益、含み損益、勝率推移）
  2. 戦略別成績（勝率・平均利益・平均損失）
  3. リスクリワード分析（平均利確額 vs 平均損切額）
  4. 卒業条件進捗（graduation_checker.pyの結果を取り込み）
  5. 改善提言（成績が悪い戦略の警告、リスクリワード比改善の提案）
  6. レポート出力（daily_investment_review.md）

使い方:
  python3 investment_reviewer.py              # レビュー生成＋Markdown出力
  python3 investment_reviewer.py --json       # JSON形式で標準出力
  python3 investment_reviewer.py --no-price   # 現在価格取得をスキップ（高速）
  python3 investment_reviewer.py --notify     # Discord通知も送信
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent

PORTFOLIO_FILE = PROJECT_ROOT / "paper_portfolio.json"
TRADE_HISTORY_FILE = PROJECT_ROOT / "trade_history.json"
PORTFOLIO_LOG_FILE = PROJECT_ROOT / "paper_portfolio_log.json"
CONFIG_FILE = PROJECT_ROOT / "crypto_config.json"
REVIEW_OUTPUT_FILE = PROJECT_ROOT / "daily_investment_review.md"
EVENT_CALENDAR_FILE = PROJECT_ROOT / "event_calendar.json"


# ==============================================================
# データ読み込み
# ==============================================================

def _load_json(path: Path) -> Any:
    """JSONファイルを読み込む。存在しなければNoneを返す。"""
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_usdjpy() -> float:
    """USDJPY レートを取得。失敗時は150.0。"""
    try:
        import yfinance as yf
        data = yf.download("USDJPY=X", period="1d", progress=False)
        val = data["Close"].dropna().iloc[-1]
        return float(val) if not hasattr(val, "__len__") else float(val.iloc[0])
    except Exception:
        return 150.0


def _get_current_prices(codes: list[str]) -> dict[str, float]:
    """yfinanceで現在価格を一括取得。"""
    try:
        import yfinance as yf
        data = yf.download(codes, period="2d", progress=False, auto_adjust=True)
        prices = {}
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
        for code in codes:
            try:
                if hasattr(close, "columns"):
                    col = code if code in close.columns else None
                    if col:
                        val = float(close[col].dropna().iloc[-1])
                        prices[code] = val
                else:
                    prices[code] = float(close.dropna().iloc[-1])
            except Exception:
                pass
        return prices
    except Exception:
        return {}


# ==============================================================
# 1. 日次サマリー
# ==============================================================

def build_daily_summary(portfolio: dict, portfolio_log: list,
                        prices: dict, usdjpy: float) -> dict:
    """ポートフォリオの日次サマリーを生成する。"""
    positions = portfolio.get("positions", [])
    closed = portfolio.get("closed_trades", [])
    initial_capital = portfolio.get("initial_capital_jpy", 300000.0)
    realized_pnl = portfolio.get("total_realized_pnl", 0.0)
    total_trades = portfolio.get("total_trades", 0)
    winning_trades = portfolio.get("winning_trades", 0)
    losing_trades = portfolio.get("losing_trades", 0)

    # 含み損益の計算
    unrealized_pnl_jpy = 0.0
    for p in positions:
        code = p["code"]
        entry_price = p["entry_price"]
        shares = p["shares"]
        side = p.get("side", "long")
        market = p.get("market", "us")
        current_price = prices.get(code, entry_price)

        if side == "long":
            pnl = (current_price - entry_price) * shares
        else:
            pnl = (entry_price - current_price) * shares

        # JPY換算
        if market in ("us", "gold"):
            pnl *= usdjpy
        # jp, btc(JPY建て), crypto はそのまま

        unrealized_pnl_jpy += pnl

    total_value = initial_capital + realized_pnl + unrealized_pnl_jpy
    total_return_pct = (total_value - initial_capital) / initial_capital * 100

    # 当日の決済件数
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_closes = [t for t in closed if t.get("exit_date", "")[:10] == today_str]
    today_realized = sum(t.get("net_pnl_jpy", 0) for t in today_closes)

    # 勝率
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    # 運用日数
    created_at = portfolio.get("created_at", "")
    if created_at:
        days_running = (datetime.now() - datetime.fromisoformat(created_at)).days
    else:
        days_running = 0

    # ドローダウン計算
    max_dd = 0.0
    if portfolio_log:
        peak = 0.0
        for r in portfolio_log:
            v = r.get("total_value_jpy", 0)
            if v > peak:
                peak = v
            if peak > 0:
                dd = (v - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd

    return {
        "date": today_str,
        "days_running": days_running,
        "position_count": len(positions),
        "today_closes": len(today_closes),
        "today_realized_pnl": round(today_realized, 0),
        "total_realized_pnl": round(realized_pnl, 0),
        "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
        "total_value_jpy": round(total_value, 0),
        "total_return_pct": round(total_return_pct, 2),
        "initial_capital": initial_capital,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": round(win_rate, 1),
        "max_drawdown_pct": round(max_dd, 2),
    }


# ==============================================================
# 2. 戦略別成績
# ==============================================================

def build_strategy_stats(closed_trades: list) -> list[dict]:
    """決済済みトレードから戦略別の成績を集計する。"""
    if not closed_trades:
        return []

    stats: dict[str, dict] = {}
    for t in closed_trades:
        strategy = t.get("strategy", "unknown")
        if strategy not in stats:
            stats[strategy] = {
                "strategy": strategy,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "win_pnls": [],
                "loss_pnls": [],
            }
        s = stats[strategy]
        pnl = t.get("net_pnl_jpy", 0)
        s["trades"] += 1
        s["total_pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
            s["win_pnls"].append(pnl)
        else:
            s["losses"] += 1
            s["loss_pnls"].append(pnl)

    result = []
    for s in stats.values():
        win_rate = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
        avg_win = (sum(s["win_pnls"]) / len(s["win_pnls"])) if s["win_pnls"] else 0
        avg_loss = (sum(s["loss_pnls"]) / len(s["loss_pnls"])) if s["loss_pnls"] else 0

        result.append({
            "strategy": s["strategy"],
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_jpy": round(s["total_pnl"], 0),
            "avg_win_jpy": round(avg_win, 0),
            "avg_loss_jpy": round(avg_loss, 0),
        })

    return sorted(result, key=lambda x: x["total_pnl_jpy"], reverse=True)


# ==============================================================
# 3. リスクリワード分析
# ==============================================================

def build_risk_reward(closed_trades: list) -> dict:
    """全決済トレードからリスクリワード比を算出する。"""
    if not closed_trades:
        return {"ratio": 0, "avg_win": 0, "avg_loss": 0, "status": "データなし"}

    wins = [t["net_pnl_jpy"] for t in closed_trades if t.get("net_pnl_jpy", 0) >= 0]
    losses = [t["net_pnl_jpy"] for t in closed_trades if t.get("net_pnl_jpy", 0) < 0]

    avg_win = (sum(wins) / len(wins)) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0

    if avg_loss > 0:
        ratio = avg_win / avg_loss
    elif avg_win > 0:
        ratio = float("inf")
    else:
        ratio = 0

    # 理想は1.5以上、1.0未満は危険
    if ratio >= 1.5:
        status = "良好"
    elif ratio >= 1.0:
        status = "改善余地あり"
    elif ratio > 0:
        status = "危険（利確額 < 損切額）"
    else:
        status = "判定不可"

    return {
        "ratio": round(ratio, 2) if ratio != float("inf") else "無限大",
        "avg_win_jpy": round(avg_win, 0),
        "avg_loss_jpy": round(avg_loss, 0),
        "win_count": len(wins),
        "loss_count": len(losses),
        "status": status,
    }


# ==============================================================
# 4. 卒業条件進捗
# ==============================================================

def build_graduation_progress(portfolio: dict, portfolio_log: list) -> dict:
    """graduation_checker.pyのロジックを簡易的に取り込む。"""
    config = _load_json(CONFIG_FILE)
    if not config:
        return {"status": "設定ファイルなし", "checks": []}

    try:
        from graduation_checker import run_graduation_check
        result = run_graduation_check(config)
        return {
            "graduated": result["graduated"],
            "passed_count": result["passed_count"],
            "total_checks": result["total_checks"],
            "checks": result["checks"],
        }
    except Exception as e:
        return {"status": f"卒業判定エラー: {e}", "checks": []}


# ==============================================================
# 4b. イベントカレンダー警告
# ==============================================================

WARN_DAYS = 3  # 何日前から警告するか


def build_event_warnings(positions: list, prices: dict, usdjpy: float) -> list[dict]:
    """event_calendar.json を読み、3日以内のイベントに対する警告を生成する。

    戻り値の各要素:
        {"name": str, "date": str, "days_left": int, "impact": str,
         "markets": list[str], "affected_positions": int,
         "affected_unrealized_jpy": float}
    """
    cal = _load_json(EVENT_CALENDAR_FILE)
    if not cal or "events" not in cal:
        return []

    today = datetime.now().date()
    warnings: list[dict] = []

    for ev in cal["events"]:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue

        days_left = (ev_date - today).days
        if days_left < 0 or days_left > WARN_DAYS:
            continue

        # 該当市場のポジションを抽出
        ev_markets = set(ev.get("markets", []))
        affected_pnl = 0.0
        affected_count = 0

        for p in positions:
            p_market = p.get("market", "")
            if p_market not in ev_markets:
                continue
            affected_count += 1

            code = p["code"]
            entry_price = p["entry_price"]
            shares = p["shares"]
            side = p.get("side", "long")
            current_price = prices.get(code, entry_price)

            if side == "long":
                pnl = (current_price - entry_price) * shares
            else:
                pnl = (entry_price - current_price) * shares

            if p_market in ("us", "gold"):
                pnl *= usdjpy

            affected_pnl += pnl

        warnings.append({
            "name": ev["name"],
            "date": ev["date"],
            "days_left": days_left,
            "impact": ev.get("impact", "medium"),
            "markets": ev.get("markets", []),
            "affected_positions": affected_count,
            "affected_unrealized_jpy": round(affected_pnl, 0),
        })

    # 日付が近い順
    warnings.sort(key=lambda w: w["days_left"])
    return warnings


# ==============================================================
# 5. 改善提言
# ==============================================================

def build_recommendations(summary: dict, strategy_stats: list,
                          risk_reward: dict, graduation: dict) -> list[str]:
    """成績データから改善提言を生成する。"""
    recs = []

    # リスクリワード比が1.0未満
    rr = risk_reward.get("ratio", 0)
    if isinstance(rr, (int, float)) and 0 < rr < 1.0:
        avg_win = risk_reward.get("avg_win_jpy", 0)
        avg_loss = risk_reward.get("avg_loss_jpy", 0)
        recs.append(
            f"[緊急] リスクリワード比 {rr} は危険水準。"
            f"平均利確 {avg_win:+,.0f}円 vs 平均損切 {avg_loss:,.0f}円。"
            f"利確幅の拡大（+3%→+5%）または損切幅の縮小を検討せよ。"
            f"RESEARCH_BACKLOG #9「損切/利確閾値の攻め化」を優先的に調査すべし。"
        )

    # 勝率が50%未満で10件以上
    if summary.get("total_trades", 0) >= 10 and summary.get("win_rate_pct", 0) < 50:
        recs.append(
            f"[警告] 勝率 {summary['win_rate_pct']}% は低水準。"
            f"エントリー条件の精度向上が必要。ファンダフィルターの見直しを検討。"
        )

    # 戦略別: 勝率0%の戦略
    for s in strategy_stats:
        if s["trades"] >= 3 and s["win_rate_pct"] == 0:
            recs.append(
                f"[警告] 戦略「{s['strategy']}」は{s['trades']}件全敗。"
                f"累計損失 {s['total_pnl_jpy']:+,.0f}円。戦略の停止または改修を検討。"
            )

    # 戦略別: 損失が大きい戦略
    for s in strategy_stats:
        if s["total_pnl_jpy"] < -1000:
            recs.append(
                f"[注意] 戦略「{s['strategy']}」の累計損失が"
                f" {s['total_pnl_jpy']:+,.0f}円に到達。"
                f"勝率{s['win_rate_pct']}%、{s['trades']}件中{s['losses']}敗。"
            )

    # ドローダウンが-10%超
    dd = summary.get("max_drawdown_pct", 0)
    if dd < -10:
        recs.append(
            f"[緊急] 最大ドローダウン {dd:.1f}% が危険域。"
            f"卒業条件の-15%に近づいている。ポジション縮小を検討。"
        )

    # 含み損が資本の5%超
    unrealized = summary.get("unrealized_pnl_jpy", 0)
    capital = summary.get("initial_capital", 300000)
    if unrealized < 0 and abs(unrealized) > capital * 0.05:
        recs.append(
            f"[注意] 含み損 {unrealized:+,.0f}円（資本の"
            f"{abs(unrealized)/capital*100:.1f}%）。"
            f"損切りラインに近いポジションの監視を強化せよ。"
        )

    # 決済が少ない（7日以上運用して5件未満）
    if summary.get("days_running", 0) >= 7 and summary.get("total_trades", 0) < 5:
        recs.append(
            f"[情報] 運用{summary['days_running']}日で決済{summary['total_trades']}件。"
            f"サンプル数が少なく戦略評価が困難。卒業判定に必要な20件まで遠い。"
        )

    # 卒業条件の未達項目
    checks = graduation.get("checks", [])
    failed = [c for c in checks if not c.get("passed", False)]
    if failed:
        names = "、".join(c["name"] for c in failed)
        recs.append(
            f"[卒業] 未達項目: {names}。"
            f"卒業には全{graduation.get('total_checks', 5)}項目のクリアが必要。"
        )

    # 問題なし
    if not recs:
        recs.append("[良好] 特記すべき問題なし。現行戦略を継続。")

    return recs


# ==============================================================
# 6. レポート出力（Markdown）
# ==============================================================

def generate_markdown_report(summary: dict, strategy_stats: list,
                             risk_reward: dict, graduation: dict,
                             recommendations: list[str],
                             event_warnings: list[dict] | None = None) -> str:
    """日次レビューをMarkdown形式で生成する。"""
    lines = []
    now = datetime.now()

    lines.append(f"# 投資部門 日次レビュー ({now.strftime('%Y-%m-%d %H:%M')})")
    lines.append("")
    lines.append(f"**運用{summary['days_running']}日目** | "
                 f"卒業期限まで残り{max(0, (datetime(2026, 4, 12) - now).days)}日")
    lines.append("")

    # --- 日次サマリー ---
    lines.append("## 日次サマリー")
    lines.append("")
    lines.append(f"| 項目 | 値 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 総資産 | {summary['total_value_jpy']:,.0f} 円 |")
    lines.append(f"| 初期資金 | {summary['initial_capital']:,.0f} 円 |")
    lines.append(f"| 累計リターン | {summary['total_return_pct']:+.2f}% |")
    lines.append(f"| 実現損益（累計） | {summary['total_realized_pnl']:+,.0f} 円 |")
    lines.append(f"| 含み損益 | {summary['unrealized_pnl_jpy']:+,.0f} 円 |")
    lines.append(f"| 本日決済 | {summary['today_closes']}件 ({summary['today_realized_pnl']:+,.0f} 円) |")
    lines.append(f"| 保有ポジション | {summary['position_count']}件 |")
    lines.append(f"| 勝率 | {summary['win_rate_pct']}% ({summary['winning_trades']}勝{summary['losing_trades']}敗 / {summary['total_trades']}件) |")
    lines.append(f"| 最大DD | {summary['max_drawdown_pct']:.2f}% |")
    lines.append("")

    # --- 戦略別成績 ---
    lines.append("## 戦略別成績")
    lines.append("")
    if strategy_stats:
        lines.append("| 戦略 | 件数 | 勝率 | 累計損益 | 平均利確 | 平均損切 |")
        lines.append("|------|------|------|---------|---------|---------|")
        for s in strategy_stats:
            lines.append(
                f"| {s['strategy']} | {s['trades']} | "
                f"{s['win_rate_pct']}% | "
                f"{s['total_pnl_jpy']:+,.0f}円 | "
                f"{s['avg_win_jpy']:+,.0f}円 | "
                f"{s['avg_loss_jpy']:+,.0f}円 |"
            )
        lines.append("")
    else:
        lines.append("決済実績なし。戦略評価は決済後に開始される。")
        lines.append("")

    # --- リスクリワード分析 ---
    lines.append("## リスクリワード分析")
    lines.append("")
    lines.append(f"- リスクリワード比: **{risk_reward['ratio']}** ({risk_reward['status']})")
    lines.append(f"- 平均利確額: {risk_reward['avg_win_jpy']:+,.0f} 円 ({risk_reward['win_count']}件)")
    lines.append(f"- 平均損切額: {risk_reward['avg_loss_jpy']:,.0f} 円 ({risk_reward['loss_count']}件)")
    lines.append("")

    # --- 卒業条件進捗 ---
    lines.append("## 卒業条件進捗")
    lines.append("")
    checks = graduation.get("checks", [])
    if checks:
        passed = sum(1 for c in checks if c.get("passed"))
        total = len(checks)
        lines.append(f"**{passed}/{total} クリア**")
        lines.append("")
        for c in checks:
            mark = "PASS" if c["passed"] else "FAIL"
            lines.append(f"- [{mark}] {c['name']}: {c['actual']}（基準: {c['required']}）")
        lines.append("")
    else:
        status = graduation.get("status", "")
        lines.append(f"卒業判定未実行: {status}")
        lines.append("")

    # --- イベント警告 ---
    if event_warnings:
        lines.append("## イベント警告")
        lines.append("")
        for w in event_warnings:
            impact_mark = {"high": "!!!", "medium": "!!", "low": "!"}.get(w["impact"], "!")
            markets_str = ", ".join(w["markets"])
            if w["days_left"] == 0:
                timing = "本日"
            else:
                timing = f"あと{w['days_left']}日"
            lines.append(
                f"- [{impact_mark}] **{w['name']}**（{w['date']}、{timing}）"
                f" — 対象市場: {markets_str}"
                f" / 該当ポジション{w['affected_positions']}件"
                f"（含み損益: {w['affected_unrealized_jpy']:+,.0f}円）"
            )
        lines.append("")

    # --- 改善提言 ---
    lines.append("## 改善提言")
    lines.append("")
    for i, rec in enumerate(recommendations, 1):
        lines.append(f"{i}. {rec}")
    lines.append("")

    # --- フッタ ---
    lines.append("---")
    lines.append(f"生成: investment_reviewer.py | {now.isoformat()}")

    return "\n".join(lines)


# ==============================================================
# 7. 統合: レビュー全体を生成
# ==============================================================

def run_review(fetch_prices: bool = True) -> dict:
    """レビューデータを一括生成して辞書で返す。
    dashboard_web.pyのAPIや、CLIからのJSON出力で使う。
    """
    portfolio = _load_json(PORTFOLIO_FILE) or {}
    trade_history = _load_json(TRADE_HISTORY_FILE) or {}
    portfolio_log = _load_json(PORTFOLIO_LOG_FILE) or []
    if not isinstance(portfolio_log, list):
        portfolio_log = []

    # 現在価格の取得
    positions = portfolio.get("positions", [])
    codes = [p["code"] for p in positions]

    if fetch_prices and codes:
        usdjpy = _get_usdjpy()
        prices = _get_current_prices(codes)
    else:
        usdjpy = 150.0
        prices = {}

    # 決済済みトレード（portfolio内のclosed_tradesを使う）
    closed_trades = portfolio.get("closed_trades", [])

    # 各分析の実行
    summary = build_daily_summary(portfolio, portfolio_log, prices, usdjpy)
    strategy_stats = build_strategy_stats(closed_trades)
    risk_reward = build_risk_reward(closed_trades)
    graduation = build_graduation_progress(portfolio, portfolio_log)
    event_warnings = build_event_warnings(positions, prices, usdjpy)
    recommendations = build_recommendations(summary, strategy_stats, risk_reward, graduation)

    return {
        "timestamp": datetime.now().isoformat(),
        "summary": summary,
        "strategy_stats": strategy_stats,
        "risk_reward": risk_reward,
        "graduation": graduation,
        "event_warnings": event_warnings,
        "recommendations": recommendations,
    }


# ==============================================================
# Discord通知
# ==============================================================

def notify_review(review: dict) -> bool:
    """レビュー結果の要約をDiscordに通知する。"""
    try:
        from notifier import send_discord_embed
    except ImportError:
        print("  [NOTIFY] notifier.pyが見つかりません")
        return False

    s = review["summary"]
    rr = review["risk_reward"]
    recs = review["recommendations"]

    # 色: 利益なら緑、損失なら赤
    color = 0x00FF00 if s["total_return_pct"] >= 0 else 0xFF0000

    fields = [
        {"name": "総資産", "value": f"{s['total_value_jpy']:,.0f}円 ({s['total_return_pct']:+.2f}%)", "inline": True},
        {"name": "実現損益", "value": f"{s['total_realized_pnl']:+,.0f}円", "inline": True},
        {"name": "含み損益", "value": f"{s['unrealized_pnl_jpy']:+,.0f}円", "inline": True},
        {"name": "勝率", "value": f"{s['win_rate_pct']}% ({s['total_trades']}件)", "inline": True},
        {"name": "RR比", "value": f"{rr['ratio']} ({rr['status']})", "inline": True},
        {"name": "本日決済", "value": f"{s['today_closes']}件 ({s['today_realized_pnl']:+,.0f}円)", "inline": True},
    ]

    # イベント警告
    evt_warns = review.get("event_warnings", [])
    if evt_warns:
        evt_lines = []
        for w in evt_warns:
            timing = "本日" if w["days_left"] == 0 else f"あと{w['days_left']}日"
            evt_lines.append(
                f"{w['name']}（{timing}）{w['affected_positions']}件"
                f"（{w['affected_unrealized_jpy']:+,.0f}円）"
            )
        fields.append({"name": "イベント警告", "value": "\n".join(evt_lines)[:1000], "inline": False})

    # 提言は先頭2件まで
    rec_text = "\n".join(recs[:2]) if recs else "特記事項なし"
    fields.append({"name": "提言", "value": rec_text[:1000], "inline": False})

    return send_discord_embed(
        title=f"日次投資レビュー ({s['date']})",
        description=f"運用{s['days_running']}日目 | ポジション{s['position_count']}件",
        color=color,
        fields=fields,
        username="相場見立方",
    )


# ==============================================================
# CLI出力
# ==============================================================

def print_review(review: dict):
    """レビュー結果をターミナルに表示する。"""
    s = review["summary"]
    print()
    print("=" * 60)
    print("  投資部門 日次レビュー")
    print("=" * 60)
    print(f"  日付: {s['date']}  運用{s['days_running']}日目")
    print(f"  総資産: {s['total_value_jpy']:,.0f}円 ({s['total_return_pct']:+.2f}%)")
    print(f"  実現損益: {s['total_realized_pnl']:+,.0f}円 | 含み損益: {s['unrealized_pnl_jpy']:+,.0f}円")
    print(f"  本日決済: {s['today_closes']}件 ({s['today_realized_pnl']:+,.0f}円)")
    print(f"  勝率: {s['win_rate_pct']}% ({s['winning_trades']}勝{s['losing_trades']}敗)")
    print(f"  最大DD: {s['max_drawdown_pct']:.2f}%")
    print()

    # 戦略別
    stats = review["strategy_stats"]
    if stats:
        print("  [戦略別]")
        for st in stats:
            print(f"    {st['strategy']:<16} {st['trades']}件 "
                  f"勝率{st['win_rate_pct']}% "
                  f"累計{st['total_pnl_jpy']:+,.0f}円")
        print()

    # リスクリワード
    rr = review["risk_reward"]
    print(f"  [RR比] {rr['ratio']} ({rr['status']})")
    print(f"    平均利確: {rr['avg_win_jpy']:+,.0f}円 / 平均損切: {rr['avg_loss_jpy']:,.0f}円")
    print()

    # 卒業条件
    grad = review["graduation"]
    checks = grad.get("checks", [])
    if checks:
        passed = sum(1 for c in checks if c.get("passed"))
        print(f"  [卒業] {passed}/{len(checks)} クリア")
        for c in checks:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"    [{mark}] {c['name']}: {c['actual']}")
        print()

    # イベント警告
    evt_warns = review.get("event_warnings", [])
    if evt_warns:
        print("  [イベント警告]")
        for w in evt_warns:
            timing = "本日" if w["days_left"] == 0 else f"あと{w['days_left']}日"
            markets_str = ", ".join(w["markets"])
            print(f"    {w['name']}（{w['date']}、{timing}）"
                  f" 対象: {markets_str}"
                  f" / {w['affected_positions']}件"
                  f"（{w['affected_unrealized_jpy']:+,.0f}円）")
        print()

    # 提言
    print("  [提言]")
    for rec in review["recommendations"]:
        print(f"    {rec}")
    print()
    print("=" * 60)


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="投資部門 日次評価・戦略レビュー自動生成"
    )
    parser.add_argument("--json", action="store_true",
                        help="JSON形式で標準出力")
    parser.add_argument("--no-price", action="store_true",
                        help="現在価格取得をスキップ（高速モード）")
    parser.add_argument("--notify", action="store_true",
                        help="Discord通知を送信")
    parser.add_argument("--no-file", action="store_true",
                        help="Markdownファイル出力をスキップ")
    args = parser.parse_args()

    # レビュー生成
    review = run_review(fetch_prices=not args.no_price)

    # JSON出力
    if args.json:
        print(json.dumps(review, indent=2, ensure_ascii=False, default=str))
        sys.exit(0)

    # ターミナル表示
    print_review(review)

    # Markdownファイル出力
    if not args.no_file:
        md = generate_markdown_report(
            review["summary"],
            review["strategy_stats"],
            review["risk_reward"],
            review["graduation"],
            review["recommendations"],
            event_warnings=review.get("event_warnings", []),
        )
        REVIEW_OUTPUT_FILE.write_text(md, encoding="utf-8")
        print(f"  レポート保存: {REVIEW_OUTPUT_FILE}")

    # Discord通知
    if args.notify:
        ok = notify_review(review)
        print(f"  Discord通知: {'成功' if ok else '失敗'}")

    sys.exit(0)


if __name__ == "__main__":
    main()

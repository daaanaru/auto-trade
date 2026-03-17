#!/usr/bin/env python3
"""
投資部 Web ダッシュボード
Flask + Jinja2 による軽量Webサーバー

起動: python3 dashboard_web.py
URL:  http://localhost:8090

エンドポイント:
    GET /              ダッシュボードトップ（ポジション一覧 + 資産状況）
    GET /api/portfolio paper_portfolio.json をJSONで返す
    GET /api/signals   スクリーナー結果をJSONで返す（BUY/SELL上位）
    GET /api/history   資産推移（直近30件）をJSONで返す
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- データファイルパス ---
PORTFOLIO_FILE       = os.path.join(BASE_DIR, "paper_portfolio.json")
PORTFOLIO_LOG_FILE   = os.path.join(BASE_DIR, "paper_portfolio_log.json")
TRADE_LOG_FILE       = os.path.join(BASE_DIR, "trade_history.json")
UNIFIED_SIGNALS_FILE = os.path.join(BASE_DIR, "screening_results_unified.json")
FULLMARKET_FILE      = os.path.join(BASE_DIR, "fullmarket_scan_results.json")

# --- 現在価格キャッシュ（同一リクエスト内で使い回し、60秒で失効）---
_price_cache: dict = {}
_price_cache_time: float = 0.0
PRICE_CACHE_TTL = 60  # 秒


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_current_prices(codes: list[str]) -> dict[str, float]:
    """yfinance で現在価格を一括取得する。失敗した銘柄は辞書から除外する。"""
    global _price_cache, _price_cache_time

    now = time.time()
    if now - _price_cache_time < PRICE_CACHE_TTL and _price_cache:
        return _price_cache

    try:
        import yfinance as yf

        # yfinance ティッカー変換
        # 仮想通貨はJPY建て（XRP-JPY等）でそのまま渡す
        # 米国株・日本株もそのまま
        tickers = []
        for c in codes:
            tickers.append(c)

        data = yf.download(
            tickers,
            period="2d",
            progress=False,
            auto_adjust=True,
        )
        prices = {}
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
        for i, orig in enumerate(codes):
            ticker = tickers[i]
            try:
                if hasattr(close, "columns"):
                    col = ticker if ticker in close.columns else orig
                    val = float(close[col].dropna().iloc[-1])
                else:
                    val = float(close.dropna().iloc[-1])
                prices[orig] = val
            except Exception:
                pass

        _price_cache = prices
        _price_cache_time = now
        return prices

    except Exception:
        return {}


def _get_usdjpy() -> float:
    """USDJPY レートを取得する。失敗時は 150.0 を返す。"""
    try:
        import yfinance as yf
        data = yf.download("USDJPY=X", period="1d", progress=False)
        val = data["Close"].dropna().iloc[-1]
        return float(val) if not hasattr(val, "__len__") else float(val.iloc[0])
    except Exception:
        return 150.0


def _build_portfolio_summary():
    """ポートフォリオの現在状況を計算して返す。"""
    pf = _load_json(PORTFOLIO_FILE)
    if not pf:
        return None

    positions = pf.get("positions", [])
    cash_jpy = pf.get("cash_jpy", 0.0)
    initial_jpy = pf.get("initial_capital_jpy", 300000.0)
    realized_pnl = pf.get("total_realized_pnl", 0.0)

    # 現在価格を取得
    codes = [p["code"] for p in positions]
    usdjpy = _get_usdjpy() if codes else 150.0
    prices = _get_current_prices(codes) if codes else {}

    enriched = []
    total_position_value_jpy = 0.0
    total_unrealized_pnl_jpy = 0.0

    for p in positions:
        code = p["code"]
        market = p.get("market", "us")
        entry_price = p["entry_price"]
        shares = p["shares"]
        side = p.get("side", "long")

        current_price = prices.get(code, entry_price)  # 取得失敗時はエントリー価格で代替

        # 損益計算（方向を考慮）
        if side == "long":
            pnl_pct = (current_price - entry_price) / entry_price * 100
            position_value_usd = current_price * shares
        else:  # short
            pnl_pct = (entry_price - current_price) / entry_price * 100
            position_value_usd = entry_price * shares  # ショートはエントリー時の価値で評価

        # JPY換算
        if market == "us" or market == "gold":
            position_value_jpy = position_value_usd * usdjpy
            pnl_jpy = (current_price - entry_price) * shares * usdjpy
            if side == "short":
                pnl_jpy = -pnl_jpy
        elif market in ("btc", "crypto"):
            # 仮想通貨はJPY建て（XRP-JPY等）なのでUSDJPY変換不要
            position_value_jpy = current_price * shares
            pnl_jpy = (current_price - entry_price) * shares
            if side == "short":
                pnl_jpy = -pnl_jpy
        else:  # jp
            position_value_jpy = current_price * shares
            pnl_jpy = (current_price - entry_price) * shares
            if side == "short":
                pnl_jpy = -pnl_jpy

        # ストップロスまでの距離（損切り-3%までの余裕）
        sl_distance_pct = pnl_pct + 3.0

        total_position_value_jpy += abs(position_value_jpy)
        total_unrealized_pnl_jpy += pnl_jpy

        # エントリー日から経過日数
        try:
            entry_dt = datetime.fromisoformat(p.get("entry_date", ""))
            days_held = (datetime.now() - entry_dt).days
        except Exception:
            days_held = 0

        enriched.append({
            "code": code,
            "name": p.get("name", code),
            "market": market,
            "side": side,
            "shares": round(shares, 4),
            "entry_price": round(entry_price, 2),
            "current_price": round(current_price, 2),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_jpy": round(pnl_jpy, 0),
            "position_value_jpy": round(abs(position_value_jpy), 0),
            "sl_distance_pct": round(sl_distance_pct, 2),
            "strategy": p.get("strategy", "-"),
            "fundamental_score": p.get("fundamental_score", 0),
            "fundamental_reason": p.get("fundamental_reason", "-"),
            "days_held": days_held,
            "price_source": "live" if code in prices else "entry",
        })

    # 正しい総資産計算: 初期資金 + 確定損益 + 含み損益
    # ※ cash + position_value だとレバレッジ分を二重計上してしまうため使わない
    total_value_jpy = initial_jpy + realized_pnl + total_unrealized_pnl_jpy
    total_return_pct = (total_value_jpy - initial_jpy) / initial_jpy * 100 if initial_jpy > 0 else 0

    summary = {
        "total_value_jpy": round(total_value_jpy, 0),
        "cash_jpy": round(cash_jpy, 0),
        "position_value_jpy": round(total_position_value_jpy, 0),
        "unrealized_pnl_jpy": round(total_unrealized_pnl_jpy, 0),
        "realized_pnl_jpy": round(realized_pnl, 0),
        "total_return_pct": round(total_return_pct, 2),
        "initial_jpy": round(initial_jpy, 0),
        "position_count": len(enriched),
        "total_trades": pf.get("total_trades", 0),
        "winning_trades": pf.get("winning_trades", 0),
        "losing_trades": pf.get("losing_trades", 0),
        "last_updated": pf.get("last_updated", "-"),
        "usdjpy": round(usdjpy, 2),
        "positions": enriched,
    }
    # 勝率
    total_closed = summary["total_trades"]
    summary["win_rate_pct"] = (
        round(summary["winning_trades"] / total_closed * 100, 1)
        if total_closed > 0 else 0.0
    )
    return summary


def _build_strategy_summary(positions: list) -> list:
    """戦略別にポジションを集計する。"""
    strats = {}
    for p in positions:
        s = p.get("strategy", "不明")
        if s not in strats:
            strats[s] = {
                "strategy": s,
                "count": 0, "long": 0, "short": 0,
                "total_pnl_jpy": 0, "total_pnl_pct": 0,
                "markets": set(),
                "worst_pnl_pct": 0, "best_pnl_pct": 0,
            }
        d = strats[s]
        d["count"] += 1
        d["long" if p.get("side") == "long" else "short"] += 1
        d["total_pnl_jpy"] += p.get("pnl_jpy", 0)
        d["total_pnl_pct"] += p.get("pnl_pct", 0)
        d["markets"].add(p.get("market", ""))
        d["worst_pnl_pct"] = min(d["worst_pnl_pct"], p.get("pnl_pct", 0))
        d["best_pnl_pct"] = max(d["best_pnl_pct"], p.get("pnl_pct", 0))

    result = []
    for s in strats.values():
        s["avg_pnl_pct"] = round(s["total_pnl_pct"] / s["count"], 2) if s["count"] > 0 else 0
        s["total_pnl_jpy"] = round(s["total_pnl_jpy"], 0)
        s["worst_pnl_pct"] = round(s["worst_pnl_pct"], 2)
        s["best_pnl_pct"] = round(s["best_pnl_pct"], 2)
        s["markets"] = sorted(s["markets"])
        result.append(s)
    return sorted(result, key=lambda x: x["total_pnl_jpy"], reverse=True)


def _build_issues(summary: dict) -> list:
    """ポートフォリオの課題・アラートを検出する。"""
    issues = []
    if not summary:
        return issues

    positions = summary.get("positions", [])
    total_trades = summary.get("total_trades", 0)

    # 決済ゼロ警告
    if total_trades == 0 and len(positions) > 0:
        issues.append({
            "level": "warning",
            "title": "決済ゼロ",
            "detail": f"{len(positions)}ポジション保有中だが1件も決済されていない。戦略の評価ができない状態。",
        })

    # SL近接ポジション（1%以内）
    sl_close = [p for p in positions if p.get("sl_distance_pct", 99) < 1.0]
    if sl_close:
        codes = ", ".join(p["code"] for p in sl_close)
        issues.append({
            "level": "danger",
            "title": f"損切り接近: {len(sl_close)}件",
            "detail": f"SLまで1%未満: {codes}",
        })

    # 塩漬けポジション（5日以上）
    stale = [p for p in positions if p.get("days_held", 0) >= 5]
    if stale:
        codes = ", ".join(f'{p["code"]}({p["days_held"]}日)' for p in stale)
        issues.append({
            "level": "warning",
            "title": f"塩漬け警告: {len(stale)}件",
            "detail": f"7日で強制決済: {codes}",
        })

    # 勝率低下（10回以上決済して勝率30%未満）
    if total_trades >= 10 and summary.get("win_rate_pct", 0) < 30:
        issues.append({
            "level": "danger",
            "title": f"勝率低下: {summary['win_rate_pct']}%",
            "detail": f"{summary['winning_trades']}勝{summary['losing_trades']}敗。戦略の見直しが必要。",
        })

    # 含み損が初期資金の10%超
    unrealized = summary.get("unrealized_pnl_jpy", 0)
    initial = summary.get("initial_jpy", 300000)
    if unrealized < 0 and abs(unrealized) > initial * 0.10:
        issues.append({
            "level": "danger",
            "title": f"含み損拡大: ¥{abs(unrealized):,.0f}",
            "detail": f"初期資金の{abs(unrealized)/initial*100:.1f}%が含み損。ドローダウン拡大中。",
        })

    # データの鮮度（最終更新が2時間以上前）
    last_updated = summary.get("last_updated", "")
    if last_updated and last_updated != "-":
        try:
            lu = datetime.fromisoformat(last_updated)
            hours_ago = (datetime.now() - lu).total_seconds() / 3600
            if hours_ago > 2:
                issues.append({
                    "level": "info",
                    "title": f"データ更新遅延: {hours_ago:.1f}時間前",
                    "detail": "launchdジョブが正常に動作しているか確認。週末は市場閉鎖のため更新なし。",
                })
        except Exception:
            pass

    # 問題なし
    if not issues:
        issues.append({
            "level": "ok",
            "title": "問題なし",
            "detail": "全ポジション正常稼働中。",
        })

    return issues


def _build_trade_history(limit: int = 20) -> list:
    """取引履歴を返す。trade_history.json は {"trades": [...]} 形式。
    テンプレートが期待するフィールド名に変換する。"""
    log = _load_json(TRADE_LOG_FILE)
    if not log:
        return []
    if isinstance(log, dict):
        log = log.get("trades", [])
    if not isinstance(log, list):
        return []
    # 決済（CLOSE）のみ抽出し、テンプレートのフィールド名に変換
    result = []
    for t in log:
        if t.get("action") != "CLOSE":
            continue
        pnl_jpy = t.get("pnl", 0) or 0
        entry_price = t.get("entry_price", 0) or 0
        exit_price = t.get("price", 0) or 0
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
        side = t.get("side", "long")
        if side == "short":
            pnl_pct = -pnl_pct
        result.append({
            "exit_date": t.get("timestamp", "-"),
            "code": t.get("symbol", "-"),
            "name": t.get("name", t.get("symbol", "-")),
            "side": side,
            "strategy": t.get("strategy", "-"),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_jpy": round(pnl_jpy, 2),
            "exit_reason": t.get("reason", "-"),
        })
    return result[-limit:]


def _build_signals():
    """スクリーナー結果から BUY / SELL シグナルを抽出する。"""
    result = {"buy": [], "sell": [], "timestamp": "-", "source": "unified"}

    # unified スクリーナー結果
    unified = _load_json(UNIFIED_SIGNALS_FILE)
    if unified and isinstance(unified, dict):
        result["timestamp"] = unified.get("timestamp", "-")
        for market_data in unified.get("markets", []):
            market_name = market_data.get("market_name", market_data.get("market", ""))
            for item in market_data.get("results", []):
                signal = item.get("signal", "NEUTRAL")
                if signal in ("BUY", "SELL"):
                    entry = {
                        "code": item.get("code", ""),
                        "name": item.get("name", item.get("code", "")),
                        "market": market_name,
                        "price": item.get("price", 0),
                        "change_pct": item.get("change_pct", 0),
                        "score": item.get("score", 0),
                        "reason": item.get("reason", ""),
                    }
                    if signal == "BUY":
                        result["buy"].append(entry)
                    else:
                        result["sell"].append(entry)

    # スコア降順でソート、上位20件
    result["buy"] = sorted(result["buy"], key=lambda x: x["score"], reverse=True)[:20]
    result["sell"] = sorted(result["sell"], key=lambda x: x["score"], reverse=True)[:20]
    return result


def _build_history(limit: int = 30):
    """資産推移ログの直近 N 件を返す。"""
    log = _load_json(PORTFOLIO_LOG_FILE)
    if not log or not isinstance(log, list):
        return []
    recent = log[-limit:]
    return [
        {
            "timestamp": e.get("timestamp", ""),
            "total_value_jpy": e.get("total_value_jpy", 0),
            "unrealized_pnl_jpy": e.get("unrealized_pnl_jpy", 0),
            "realized_pnl_jpy": e.get("realized_pnl_jpy", 0),
            "total_return_pct": e.get("total_return_pct", 0),
            "position_count": e.get("position_count", 0),
        }
        for e in recent
    ]


# =====================================================================
# ルーティング
# =====================================================================

@app.route("/")
def index():
    summary = _build_portfolio_summary()
    signals = _build_signals()
    history = _build_history(30)
    strategy_summary = _build_strategy_summary(summary.get("positions", [])) if summary else []
    issues = _build_issues(summary)
    trade_history = _build_trade_history(20)
    return render_template(
        "dashboard.html",
        summary=summary,
        signals=signals,
        history=history,
        strategy_summary=strategy_summary,
        issues=issues,
        trade_history=trade_history,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.route("/strategies")
def strategies_page():
    return render_template("strategies.html")


@app.route("/analysis")
def analysis_page():
    return render_template("analysis.html")


@app.route("/api/portfolio")
def api_portfolio():
    summary = _build_portfolio_summary()
    if summary is None:
        return jsonify({"error": "portfolio data not found"}), 404
    return jsonify(summary)


@app.route("/api/signals")
def api_signals():
    return jsonify(_build_signals())


@app.route("/api/history")
def api_history():
    limit = request.args.get("limit", 30, type=int)
    return jsonify(_build_history(limit))


@app.route("/api/strategies")
def api_strategies():
    summary = _build_portfolio_summary()
    if not summary:
        return jsonify([])
    return jsonify(_build_strategy_summary(summary.get("positions", [])))


@app.route("/api/issues")
def api_issues():
    summary = _build_portfolio_summary()
    return jsonify(_build_issues(summary))


@app.route("/api/trades")
def api_trades():
    return jsonify(_build_trade_history(50))


@app.route("/api/review")
def api_review():
    """日次投資レビューをJSONで返す。
    クエリパラメータ:
      ?no_price=1  現在価格取得をスキップ（高速モード）
    """
    try:
        from investment_reviewer import run_review
        no_price = request.args.get("no_price", "0") == "1"
        review = run_review(fetch_prices=not no_price)
        return jsonify(review)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =====================================================================
# エントリポイント
# =====================================================================

if __name__ == "__main__":
    port = 8090
    print(f"投資部ダッシュボード起動中... http://localhost:{port}")
    print("終了: Ctrl+C")
    app.run(host="127.0.0.1", port=port, debug=False)

#!/usr/bin/env python3
"""
auto-trade 統合ダッシュボード

全市場のシグナル・ペーパートレード状況・卒業条件を一覧表示する。
データファイルを読み取るだけのビューア（書き込みは一切しない）。

使い方:
    python dashboard.py              # ターミナル表示（rich）
    python dashboard.py --json       # JSON出力
    python dashboard.py --no-fetch   # yfinanceアクセスなし（キャッシュのみ）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


# ---------------------------------------------------------------------------
# データ読み込みユーティリティ
# ---------------------------------------------------------------------------

def load_json(filename: str):
    """JSONファイルを安全に読み込む。存在しなければNoneを返す。"""
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def file_mtime(filename: str) -> str:
    """ファイルの最終更新日時を文字列で返す。"""
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return "---"
    ts = os.path.getmtime(path)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 1. 市場別シグナル一覧
# ---------------------------------------------------------------------------

def gather_signals(no_fetch: bool = False) -> list[dict]:
    """watchlist + BTC + 米国株 + ゴールドのシグナル情報を集める。

    no_fetch=True の場合は performance_log から最新シグナルだけ返す。
    """
    results = []

    # --- 日本株（watchlist.json + signal_monitor の analyze_symbol） ---
    watchlist = load_json("watchlist.json")
    jp_symbols = []
    if watchlist:
        jp_symbols = watchlist.get("symbols", [])

    if not no_fetch and jp_symbols:
        try:
            from signal_monitor import analyze_symbol, load_optimized_params
            params = load_optimized_params()
            for item in jp_symbols:
                r = analyze_symbol(item["code"], params)
                results.append({
                    "market": "JP",
                    "symbol": item["code"],
                    "name": item.get("name", item["code"]),
                    "signal": r["signal"],
                    "price": r["price"],
                    "score": r["score"],
                    "error": r.get("error"),
                })
        except Exception as e:
            for item in jp_symbols:
                results.append({
                    "market": "JP",
                    "symbol": item["code"],
                    "name": item.get("name", item["code"]),
                    "signal": "N/A",
                    "price": 0,
                    "score": 0,
                    "error": str(e),
                })
    elif jp_symbols:
        for item in jp_symbols:
            results.append({
                "market": "JP",
                "symbol": item["code"],
                "name": item.get("name", item["code"]),
                "signal": "N/A",
                "price": 0,
                "score": 0,
                "error": "no-fetch mode",
            })

    # --- BTC（performance_log の最新エントリ） ---
    perf = load_json("performance_log.json")
    if perf and len(perf) > 0:
        latest = perf[-1]
        signals = latest.get("signals", {})
        # 最も重要な戦略シグナルを決定（BUY/SELL > NEUTRAL）
        btc_signal = "NEUTRAL"
        for _strat, sig in signals.items():
            if sig in ("BUY", "SELL"):
                btc_signal = sig
                break
        results.append({
            "market": "BTC",
            "symbol": "BTC/JPY",
            "name": "ビットコイン",
            "signal": btc_signal,
            "price": latest.get("price_jpy", 0),
            "score": 0,
            "error": None,
        })

    # --- 米国株・ゴールド（no_fetchでなければyfinanceから取得） ---
    us_symbols = [
        ("AAPL", "Apple", "US"),
        ("NVDA", "NVIDIA", "US"),
        ("SPY", "S&P500 ETF", "US"),
        ("GC=F", "ゴールド", "GOLD"),
    ]
    if not no_fetch:
        try:
            import yfinance as yf
            from signal_monitor import load_optimized_params
            from strategies.monthly_momentum import MonthlyMomentumStrategy
            import pandas as pd

            params = load_optimized_params()
            for sym, name, market in us_symbols:
                try:
                    df = yf.download(sym, period="3mo", interval="1d", progress=False)
                    if df.empty:
                        raise ValueError("no data")
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.droplevel(1)
                    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                    df.columns = ["open", "high", "low", "close", "volume"]
                    df = df.dropna()

                    strategy = MonthlyMomentumStrategy(params=params)
                    sigs = strategy.generate_signals(df)
                    sig_val = int(sigs.iloc[-1])
                    sig_label = "BUY" if sig_val == 1 else ("SELL" if sig_val == -1 else "HOLD")
                    price = float(df["close"].iloc[-1])
                    results.append({
                        "market": market,
                        "symbol": sym,
                        "name": name,
                        "signal": sig_label,
                        "price": price,
                        "score": 0,
                        "error": None,
                    })
                except Exception as e:
                    results.append({
                        "market": market,
                        "symbol": sym,
                        "name": name,
                        "signal": "N/A",
                        "price": 0,
                        "score": 0,
                        "error": str(e),
                    })
        except ImportError as e:
            for sym, name, market in us_symbols:
                results.append({
                    "market": market,
                    "symbol": sym,
                    "name": name,
                    "signal": "N/A",
                    "price": 0,
                    "score": 0,
                    "error": str(e),
                })

    return results


# ---------------------------------------------------------------------------
# 2. ペーパートレード状況
# ---------------------------------------------------------------------------

def gather_paper_trade() -> dict:
    """ペーパートレードの現在状態を返す。"""
    positions = load_json("paper_positions.json")
    trade_log = load_json("paper_trade_log.json")
    perf = load_json("performance_log.json")

    if positions is None:
        return {"available": False}

    result = {
        "available": True,
        "capital": positions.get("capital", 0),
        "position_btc": positions.get("position", 0),
        "entry_price": positions.get("entry_price", 0),
        "total_pnl": positions.get("total_pnl", 0),
        "total_trades": positions.get("total_trades", 0),
        "winning_trades": positions.get("winning_trades", 0),
        "losing_trades": positions.get("losing_trades", 0),
        "strategy": positions.get("strategy", "---"),
        "last_updated": positions.get("last_updated", "---"),
    }

    # 勝率
    total = result["total_trades"]
    if total > 0:
        result["win_rate"] = round(result["winning_trades"] / total * 100, 1)
    else:
        result["win_rate"] = 0.0

    # 最新の評価額（performance_logから）
    if perf and len(perf) > 0:
        latest = perf[-1]
        result["current_price"] = latest.get("price_jpy", 0)
        result["total_value"] = latest.get("total_value_jpy", 0)
        result["unrealized_pnl"] = latest.get("unrealized_pnl_jpy", 0)
        result["realized_pnl"] = latest.get("realized_pnl_jpy", 0)
    else:
        result["current_price"] = 0
        result["total_value"] = result["capital"]
        result["unrealized_pnl"] = 0
        result["realized_pnl"] = 0

    # 取引ログ件数
    result["log_entries"] = len(trade_log) if trade_log else 0

    return result


# ---------------------------------------------------------------------------
# 3. 卒業条件チェック
# ---------------------------------------------------------------------------

def gather_graduation() -> dict:
    """卒業条件の達成状況を返す。"""
    config = load_json("crypto_config.json")
    positions = load_json("paper_positions.json")
    perf = load_json("performance_log.json")

    if config is None:
        return {"available": False}

    criteria = config.get("graduation_criteria", {})
    result = {
        "available": True,
        "criteria": criteria,
        "checks": {},
    }

    # ペーパートレード期間
    min_days = criteria.get("min_days", 14)
    if positions and positions.get("created_at"):
        try:
            start = datetime.fromisoformat(positions["created_at"])
            elapsed = (datetime.now() - start).days
            result["checks"]["period"] = {
                "label": f"ペーパートレード期間 >= {min_days}日",
                "value": f"{elapsed}日",
                "passed": elapsed >= min_days,
            }
        except (ValueError, TypeError):
            result["checks"]["period"] = {
                "label": f"ペーパートレード期間 >= {min_days}日",
                "value": "不明",
                "passed": False,
            }
    else:
        result["checks"]["period"] = {
            "label": f"ペーパートレード期間 >= {min_days}日",
            "value": "データなし",
            "passed": False,
        }

    # 勝率
    min_win_rate = criteria.get("min_win_rate", 40.0)
    if positions:
        total = positions.get("total_trades", 0)
        wins = positions.get("winning_trades", 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        result["checks"]["win_rate"] = {
            "label": f"勝率 >= {min_win_rate}%",
            "value": f"{win_rate:.1f}% ({wins}/{total})",
            "passed": win_rate >= min_win_rate if total > 0 else False,
        }
    else:
        result["checks"]["win_rate"] = {
            "label": f"勝率 >= {min_win_rate}%",
            "value": "データなし",
            "passed": False,
        }

    # 最大ドローダウン
    max_dd = criteria.get("max_drawdown_pct", -15.0)
    if perf and len(perf) > 1:
        values = [e.get("total_value_jpy", 0) for e in perf]
        peak = values[0]
        worst_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = ((v - peak) / peak * 100) if peak > 0 else 0
            if dd < worst_dd:
                worst_dd = dd
        result["checks"]["max_drawdown"] = {
            "label": f"最大DD >= {max_dd}%",
            "value": f"{worst_dd:.2f}%",
            "passed": worst_dd >= max_dd,
        }
    else:
        result["checks"]["max_drawdown"] = {
            "label": f"最大DD >= {max_dd}%",
            "value": "データなし",
            "passed": False,
        }

    # ローリングSharpe（簡易計算: 日次リターンから）
    min_sharpe = criteria.get("min_rolling_sharpe", 0.5)
    if perf and len(perf) > 5:
        values = [e.get("total_value_jpy", 0) for e in perf]
        returns = []
        for i in range(1, len(values)):
            if values[i - 1] > 0:
                returns.append((values[i] - values[i - 1]) / values[i - 1])
        if returns:
            import statistics
            mean_r = statistics.mean(returns)
            std_r = statistics.stdev(returns) if len(returns) > 1 else 1
            sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0
            result["checks"]["rolling_sharpe"] = {
                "label": f"ローリングSharpe >= {min_sharpe}",
                "value": f"{sharpe:.2f}",
                "passed": sharpe >= min_sharpe,
            }
        else:
            result["checks"]["rolling_sharpe"] = {
                "label": f"ローリングSharpe >= {min_sharpe}",
                "value": "計算不可",
                "passed": False,
            }
    else:
        result["checks"]["rolling_sharpe"] = {
            "label": f"ローリングSharpe >= {min_sharpe}",
            "value": "データ不足",
            "passed": False,
        }

    # 全条件達成?
    result["all_passed"] = all(c["passed"] for c in result["checks"].values())

    return result


# ---------------------------------------------------------------------------
# 4. 直近シグナル発生履歴（過去7日）
# ---------------------------------------------------------------------------

def gather_signal_history(days: int = 7) -> list[dict]:
    """performance_log から過去N日のシグナル変化を抽出する。"""
    perf = load_json("performance_log.json")
    if not perf:
        return []

    cutoff = datetime.now() - timedelta(days=days)
    history = []
    prev_signals = {}

    for entry in perf:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
        except (ValueError, KeyError):
            continue
        if ts < cutoff:
            prev_signals = entry.get("signals", {})
            continue

        current_signals = entry.get("signals", {})
        for strat, sig in current_signals.items():
            prev = prev_signals.get(strat, sig)
            if sig != prev and sig in ("BUY", "SELL"):
                history.append({
                    "timestamp": entry["timestamp"],
                    "market": "BTC",
                    "symbol": "BTC/JPY",
                    "strategy": strat,
                    "signal": sig,
                    "price": entry.get("price_jpy", 0),
                })
        prev_signals = current_signals

    return history


# ---------------------------------------------------------------------------
# Rich ターミナル表示
# ---------------------------------------------------------------------------

def render_rich(signals: list, paper: dict, graduation: dict, history: list):
    """richライブラリで色付きターミナル表示する。"""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text

    console = Console()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    console.print()
    console.print(Panel.fit(
        f"[bold]auto-trade 統合ダッシュボード[/bold]  {now}",
        border_style="blue",
    ))

    # --- 1. 市場別シグナル一覧 ---
    sig_table = Table(title="市場別シグナル一覧", show_lines=False)
    sig_table.add_column("市場", style="dim", width=6)
    sig_table.add_column("銘柄", width=28)
    sig_table.add_column("価格", justify="right", width=14)
    sig_table.add_column("シグナル", justify="center", width=10)

    def signal_style(sig: str) -> str:
        if sig == "BUY":
            return "[bold green]BUY[/bold green]"
        elif sig == "SELL":
            return "[bold red]SELL[/bold red]"
        elif sig in ("HOLD", "NEUTRAL"):
            return "[dim]HOLD[/dim]"
        return f"[yellow]{sig}[/yellow]"

    def format_price(price: float, symbol: str) -> str:
        if price == 0:
            return "---"
        if symbol.endswith(".T") or "JPY" in symbol:
            return f"{price:,.0f}"
        return f"${price:,.2f}"

    for s in signals:
        sig_display = signal_style(s["signal"])
        price_display = format_price(s["price"], s["symbol"])
        name_display = f"{s['name']} ({s['symbol']})"
        if s.get("error"):
            sig_display = f"[yellow]ERR[/yellow]"
        sig_table.add_row(s["market"], name_display, price_display, sig_display)

    console.print(sig_table)
    console.print()

    # --- 2. ペーパートレード状況 ---
    if paper["available"]:
        pt_table = Table(title="ペーパートレード (BTC/JPY)", show_lines=False)
        pt_table.add_column("項目", width=20)
        pt_table.add_column("値", justify="right", width=20)

        pnl_style = "green" if paper["total_pnl"] >= 0 else "red"
        unreal_style = "green" if paper["unrealized_pnl"] >= 0 else "red"

        pt_table.add_row("資本金", f"{paper['capital']:,.0f} JPY")
        pt_table.add_row("評価額", f"{paper['total_value']:,.0f} JPY")
        pt_table.add_row("BTC保有", f"{paper['position_btc']:.6f} BTC")
        pt_table.add_row("累計損益", f"[{pnl_style}]{paper['total_pnl']:+,.0f} JPY[/{pnl_style}]")
        pt_table.add_row("含み損益", f"[{unreal_style}]{paper['unrealized_pnl']:+,.0f} JPY[/{unreal_style}]")
        pt_table.add_row("取引回数", f"{paper['total_trades']}回")
        pt_table.add_row("勝率", f"{paper['win_rate']}%")
        pt_table.add_row("戦略", paper["strategy"])
        pt_table.add_row("最終更新", paper["last_updated"][:16] if len(paper["last_updated"]) > 16 else paper["last_updated"])

        console.print(pt_table)
    else:
        console.print("[dim]ペーパートレード: データなし[/dim]")
    console.print()

    # --- 3. 卒業条件チェック ---
    if graduation["available"]:
        grad_table = Table(title="卒業条件チェック", show_lines=False)
        grad_table.add_column("条件", width=32)
        grad_table.add_column("現在値", justify="right", width=16)
        grad_table.add_column("判定", justify="center", width=6)

        for _key, check in graduation["checks"].items():
            mark = "[green]OK[/green]" if check["passed"] else "[red]NG[/red]"
            grad_table.add_row(check["label"], check["value"], mark)

        overall = "[bold green]PASS[/bold green]" if graduation["all_passed"] else "[bold red]NOT YET[/bold red]"
        grad_table.add_row("", "", "")
        grad_table.add_row("[bold]総合判定[/bold]", "", overall)

        console.print(grad_table)
    else:
        console.print("[dim]卒業条件: データなし[/dim]")
    console.print()

    # --- 4. 直近シグナル履歴（7日） ---
    if history:
        hist_table = Table(title="直近シグナル発生履歴（過去7日）", show_lines=False)
        hist_table.add_column("日時", width=16)
        hist_table.add_column("銘柄", width=12)
        hist_table.add_column("戦略", width=10)
        hist_table.add_column("シグナル", justify="center", width=8)
        hist_table.add_column("価格", justify="right", width=14)

        for h in history[-20:]:  # 最大20件
            sig_display = signal_style(h["signal"])
            price_display = f"{h['price']:,.0f}"
            ts_display = h["timestamp"][:16]
            hist_table.add_row(ts_display, h["symbol"], h["strategy"], sig_display, price_display)

        console.print(hist_table)
    else:
        console.print("[dim]直近7日間のシグナル変化: なし[/dim]")

    console.print()
    console.print(f"[dim]データ更新: watchlist {file_mtime('watchlist.json')} / "
                  f"performance_log {file_mtime('performance_log.json')} / "
                  f"paper_positions {file_mtime('paper_positions.json')}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# JSON 出力
# ---------------------------------------------------------------------------

def render_json(signals: list, paper: dict, graduation: dict, history: list):
    """全データをJSON形式で出力する。"""
    output = {
        "timestamp": datetime.now().isoformat(),
        "signals": signals,
        "paper_trade": paper,
        "graduation": graduation,
        "signal_history": history,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="auto-trade 統合ダッシュボード")
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="JSON形式で出力",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", dest="no_fetch",
        help="yfinanceアクセスなし（ローカルデータのみ）",
    )
    args = parser.parse_args()

    signals = gather_signals(no_fetch=args.no_fetch)
    paper = gather_paper_trade()
    graduation = gather_graduation()
    history = gather_signal_history(days=7)

    if args.json_output:
        render_json(signals, paper, graduation, history)
    else:
        render_rich(signals, paper, graduation, history)


if __name__ == "__main__":
    main()

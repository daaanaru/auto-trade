"""
llm_ab_tracker.py — LLM A/Bテスト記録（結論済み・Ollama廃止）

A/Bテスト結論（2026-03-13）: シグナルのみ(A群48%)がLLM(B群30%)を上回った。
Ollamaは2026-03-24に廃止。このファイルはレポート閲覧用に残存。
record_ab()は常にSKIPを返す。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
AB_LOG_FILE = PROJECT_ROOT / "llm_ab_log.json"


def load_ab_log() -> list:
    if AB_LOG_FILE.exists():
        with open(AB_LOG_FILE, "r") as f:
            return json.load(f)
    return []


def save_ab_log(log: list):
    with open(AB_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


def get_signal_only_verdict(signals: dict) -> str:
    """シグナルのみで判断（多数決、NEUTRAL考慮版）。

    BUY/SELLのどちらかが過半数（4戦略中2票以上）必要。
    1票だけでは「多数決」として成立しないためHOLDを返す。
    """
    buy = sum(1 for s in signals.values() if s["label"] == "BUY")
    sell = sum(1 for s in signals.values() if s["label"] == "SELL")
    total = len(signals)
    threshold = total / 2  # 4戦略なら2票必要

    if buy >= threshold and buy > sell:
        return "BUY"
    elif sell >= threshold and sell > buy:
        return "SELL"
    return "HOLD"


def get_llm_verdict(price: float, signals: dict, config: dict) -> str:
    """Ollama廃止済み（2026-03-24）。常にSKIPを返す。"""
    return "SKIP"


def record_ab(price: float, signals: dict, config: dict):
    """A/Bの判断を記録する。

    重複防止: 前回記録から60秒以内の場合はスキップする。
    launchdのcrypto-monitorとcrypto-full-reportが同時刻に走ると
    155ms差で二重記録される問題への対策。
    """
    # A/Bテスト結論済み（2026-03-13）、Ollama廃止（2026-03-24）。常にスキップ
    return {"a_signal_only": "SKIP", "b_with_llm": "SKIP", "agree": True}

    log = load_ab_log()

    # 重複チェック: 前回記録から60秒以内ならスキップ
    if log:
        last_ts = log[-1].get("timestamp", "")
        try:
            last_time = datetime.fromisoformat(last_ts)
            elapsed = (datetime.now() - last_time).total_seconds()
            if elapsed < 60:
                print(f"  [A/B] Skipped: last record was {elapsed:.0f}s ago (< 60s)")
                return log[-1]  # 前回のエントリーを返す
        except (ValueError, TypeError):
            pass

    signal_verdict = get_signal_only_verdict(signals)
    llm_verdict = get_llm_verdict(price, signals, config)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "price": price,
        "signals": {k: v["label"] for k, v in signals.items()},
        "a_signal_only": signal_verdict,
        "b_with_llm": llm_verdict,
        "agree": signal_verdict == llm_verdict,
        # 次回実行時に埋める（価格変動で正解を判定）
        "outcome": None,
    }

    # 前回のエントリーにoutcomeを埋める
    if log:
        prev = log[-1]
        if prev.get("outcome") is None and prev.get("price"):
            price_change = (price - prev["price"]) / prev["price"] * 100
            if price_change > 0.3:
                actual = "BUY"
            elif price_change < -0.3:
                actual = "SELL"
            else:
                actual = "HOLD"
            prev["outcome"] = actual
            prev["price_change_pct"] = round(price_change, 3)
            prev["a_correct"] = prev["a_signal_only"] == actual
            prev["b_correct"] = prev["b_with_llm"] == actual

    log.append(entry)
    save_ab_log(log)

    return entry


def generate_ab_report() -> str:
    """A/Bテストの成績比較レポートを生成する。"""
    log = load_ab_log()
    evaluated = [e for e in log if e.get("outcome") is not None]

    if len(evaluated) < 3:
        return f"データ不足（{len(evaluated)}件）。最低3件の評価済みデータが必要。"

    a_correct = sum(1 for e in evaluated if e.get("a_correct"))
    b_correct = sum(1 for e in evaluated if e.get("b_correct"))
    agree_count = sum(1 for e in evaluated if e.get("agree"))
    total = len(evaluated)

    a_rate = a_correct / total * 100
    b_rate = b_correct / total * 100

    report = f"""## LLM A/Bテスト中間報告

| 項目 | A群（シグナルのみ） | B群（LLMあり） |
|------|-------------------|---------------|
| 正答数 | {a_correct}/{total} | {b_correct}/{total} |
| 正答率 | {a_rate:.1f}% | {b_rate:.1f}% |
| 判断一致率 | {agree_count}/{total} ({agree_count/total*100:.0f}%) | — |

評価期間: {evaluated[0]['timestamp'][:10]} 〜 {evaluated[-1]['timestamp'][:10]}
データ点数: {total}件

"""
    if b_rate > a_rate + 5:
        report += "**結論: LLMがシグナルを上回っている。**"
    elif a_rate > b_rate + 5:
        report += "**結論: シグナルのみの方が優秀。LLMを外してメモリ節約を推奨。**"
    else:
        report += "**結論: 有意差なし。データ蓄積を継続。**"

    return report


if __name__ == "__main__":
    print(generate_ab_report())

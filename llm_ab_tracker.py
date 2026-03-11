"""
llm_ab_tracker.py — LLM分析の有無によるA/Bテスト記録

毎時のcrypto_monitor実行時に:
1. シグナルのみの判断（A群: no-LLM）を記録
2. LLMに判断を聞いて（B群: with-LLM）を記録
3. 実際の価格変動と比較して、どちらが正確だったか追跡

実トレードには一切影響しない。純粋な検証用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

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
    """OllamaのLLMに判断を聞く。BUY/SELL/HOLDのみ返させる。

    v3: テクニカル指標サマリーを渡す（価格+変動率+各戦略の判断）。
    v2は価格の絶対値のみで判断不能だった（HOLD連発問題）。
    v1はシグナルをそのまま渡して追従するだけだった（100%一致問題）。
    v3はシグナルの「ラベル」ではなく「戦略名と判断理由」を渡し、LLMに独自の総合判断をさせる。
    """
    # 各戦略の判断をサマリー化（ラベルだけでなく戦略名も渡す）
    signal_lines = []
    for strategy_name, sig in signals.items():
        label = sig["label"] if isinstance(sig, dict) else sig
        signal_lines.append(f"  - {strategy_name}: {label}")
    signal_summary = "\n".join(signal_lines) if signal_lines else "  (no signals)"

    prompt = f"""You are a crypto trading analyst. Given the market data below, respond with EXACTLY one word: BUY, SELL, or HOLD.

BTC/JPY price: {price:,.0f}
Technical strategy signals (4 independent strategies):
{signal_summary}

Based on the consensus (or lack thereof) among these strategies and the current price, give your independent verdict. If strategies conflict, weigh them and decide. Your verdict (one word only):"""

    ollama_config = config.get("ollama", {})
    url = f"{ollama_config.get('base_url', 'http://localhost:11434')}/api/generate"

    try:
        resp = requests.post(
            url,
            json={
                "model": ollama_config.get("model", "qwen2.5:7b"),
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 10},
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip().upper()
        # 最初の有効な判断語を抽出
        for word in raw.split():
            if word in ("BUY", "SELL", "HOLD"):
                return word
        return "HOLD"  # パース失敗時のフォールバック
    except Exception:
        return "ERROR"


def record_ab(price: float, signals: dict, config: dict):
    """A/Bの判断を記録する。

    重複防止: 前回記録から60秒以内の場合はスキップする。
    launchdのcrypto-monitorとcrypto-full-reportが同時刻に走ると
    155ms差で二重記録される問題への対策。
    """
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
        report += "**結論: LLMがシグナルを上回っている。Ollama維持の価値あり。**"
    elif a_rate > b_rate + 5:
        report += "**結論: シグナルのみの方が優秀。LLMを外してメモリ節約を推奨。**"
    else:
        report += "**結論: 有意差なし。データ蓄積を継続。**"

    return report


if __name__ == "__main__":
    print(generate_ab_report())

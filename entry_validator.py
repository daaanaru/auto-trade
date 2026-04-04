#!/usr/bin/env python3
"""
エントリー検証レイヤー（Dexter Validation Agent 着想）

BUY/SHORT候補に対してLLMで自己検証を行い、妥当性の低いエントリーをフィルタする。
claude -p（Claude Max契約内、追加費用なし）を使用。

設計思想:
- Dexterの4エージェント構成のうち Validation Agent の役割だけを抽出
- テクニカル+ファンダの整合性、市場環境、RR比を検証
- 候補をバッチで渡し、1回のLLM呼び出しで全件判定（速度重視）
- 検証結果はログに記録し、後で精度を測定可能にする
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VALIDATION_LOG_FILE = os.path.join(BASE_DIR, "validation_log.json")

# 検証をスキップする最小候補数（1件だけの時もやる）
MIN_CANDIDATES_FOR_VALIDATION = 0

# claude -p のタイムアウト（秒）
CLAUDE_TIMEOUT = 120


def _fail_safe_reject(candidates: list, reason: str):
    """LLM検証が失敗した際に、全候補をshadow logに記録する。
    ログ書き込み失敗でもクラッシュしない。"""
    try:
        shadow_log_file = os.path.join(BASE_DIR, "validation_shadow_log.json")
        entry = {
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "rejected_candidates": [
                {"code": c.get("code"), "name": c.get("name"), "market": c.get("market"),
                 "strategy": c.get("strategy"), "price": c.get("price")}
                for c in candidates
            ],
        }
        log = []
        if os.path.exists(shadow_log_file):
            try:
                with open(shadow_log_file, "r") as f:
                    log = json.load(f)
            except (json.JSONDecodeError, IOError):
                log = []
        log.append(entry)
        if len(log) > 200:
            log = log[-200:]
        with open(shadow_log_file, "w") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as e:
        # ログ書き込み失敗でもクラッシュさせない
        logger.warning(f"shadow log書き込み失敗: {e}")


def _build_validation_prompt(
    candidates: list,
    portfolio_summary: dict,
    regime: str = "UNKNOWN",
) -> str:
    """検証用プロンプトを組み立てる。"""

    candidates_text = ""
    for i, c in enumerate(candidates, 1):
        side = "LONG" if c.get("signal", 1) == 1 else "SHORT"
        funda = c.get("fundamental", {})
        candidates_text += (
            f"\n{i}. {c['name']}({c['code']}) | {c['market'].upper()} | {side}"
            f" | 価格: {c['price']:,.2f} | 戦略: {c['strategy']}"
            f" | ファンダスコア: {funda.get('score', 'N/A')}"
            f" | ファンダ理由: {funda.get('reason', 'N/A')}"
        )

    prompt = f"""あなたは金融トレードの検証エージェントです。
以下のエントリー候補を厳密に検証し、各候補にPASS/REJECTの判定を下してください。

## ポートフォリオ状況
- 現金: {portfolio_summary.get('cash_jpy', 0):,.0f} JPY
- 保有ポジション数: {portfolio_summary.get('position_count', 0)}件
- 総資産: {portfolio_summary.get('total_value', 0):,.0f} JPY
- 確定損益: {portfolio_summary.get('realized_pnl', 0):+,.0f} JPY
- 市場レジーム: {regime}

## エントリー候補
{candidates_text}

## 検証基準（以下のいずれかに該当したらREJECT）
1. テクニカルとファンダの矛盾: BUYシグナルなのにファンダスコアが低すぎる（0.0ギリギリ）
2. 集中リスク: 同一セクター・市場に偏りすぎ（既存ポジションとの重複）
3. ボラティリティ不整合: 低ボラ銘柄にSHORTは利幅が取れない
4. タイミングリスク: 決算直前・配当落ち直後など不利なタイミング
5. 流動性リスク: 出来高が極端に少ない銘柄

## 回答フォーマット（必ずこのJSON形式で回答。他の文章は一切不要）
```json
[
  {{"code": "銘柄コード", "verdict": "PASS" or "REJECT", "confidence": 0.0-1.0, "reason": "判定理由（1行）"}}
]
```
"""
    return prompt


def validate_entries(
    candidates: list,
    portfolio: dict,
    regime: str = "UNKNOWN",
    dry_run: bool = False,
) -> list:
    """候補リストをLLMで検証し、PASS判定の候補だけを返す。

    Args:
        candidates: buy_candidates or short_candidates のリスト
        portfolio: 現在のポートフォリオ
        regime: 市場レジーム文字列
        dry_run: Trueなら検証結果を表示するだけで全候補を通す

    Returns:
        検証を通過した候補のリスト（元のdictに validation フィールドを追加）
    """
    if not candidates:
        return []

    # ポートフォリオサマリーを作成
    portfolio_summary = {
        "cash_jpy": portfolio.get("cash_jpy", 0),
        "position_count": len(portfolio.get("positions", [])),
        "total_value": portfolio.get("initial_capital_jpy", 300000)
            + portfolio.get("total_realized_pnl", 0),
        "realized_pnl": portfolio.get("total_realized_pnl", 0),
    }

    prompt = _build_validation_prompt(candidates, portfolio_summary, regime)

    # claude -p で検証実行
    try:
        print(f"  [Validation] LLM検証中... ({len(candidates)}候補)")
        start_time = time.time()

        # CLAUDE_CODE環境変数を除外（二重起動チェック回避）
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        # launchd環境はPATHが最小限（/usr/bin:/bin）のため、Homebrew等を明示追加
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")

        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
            cwd=BASE_DIR,
        )

        elapsed = time.time() - start_time
        print(f"  [Validation] LLM応答完了 ({elapsed:.1f}秒)")

        if result.returncode != 0:
            logger.warning(f"claude -p failed (rc={result.returncode}): {result.stderr[:200]}")
            print(f"  [Validation] LLM検証失敗、全候補をブロック（fail-closed）")
            _fail_safe_reject(candidates, "LLM実行失敗(rc={})".format(result.returncode))
            if dry_run:
                return candidates
            return []

        # claude --output-format json は {"result": "..."} を返す
        try:
            outer = json.loads(result.stdout)
            response_text = outer.get("result", result.stdout)
        except (json.JSONDecodeError, AttributeError):
            response_text = result.stdout

        # JSONブロックを抽出
        verdicts = _parse_verdicts(response_text)

        if not verdicts:
            logger.warning("検証結果のパースに失敗、全候補をブロック（fail-closed）")
            print(f"  [Validation] パース失敗、全候補をブロック（fail-closed）")
            _fail_safe_reject(candidates, "LLM応答パース失敗")
            if dry_run:
                return candidates
            return []

        # 検証結果をマッピング
        verdict_map = {v["code"]: v for v in verdicts}
        passed = []
        rejected = []

        for candidate in candidates:
            code = candidate["code"]
            v = verdict_map.get(code)

            if v is None:
                # 検証結果に含まれない候補はREJECT（fail-closed）
                candidate["validation"] = {
                    "verdict": "REJECT",
                    "confidence": 0.0,
                    "reason": "LLM応答に含まれず（fail-closed: デフォルトREJECT）",
                }
                rejected.append(candidate)
                print(f"  [REJECT] {candidate['name']}({code}) | LLM応答に含まれず（fail-closed）")
                continue

            candidate["validation"] = {
                "verdict": v.get("verdict", "PASS"),
                "confidence": v.get("confidence", 0.5),
                "reason": v.get("reason", ""),
            }

            if v.get("verdict") == "REJECT":
                rejected.append(candidate)
                print(f"  [REJECT] {candidate['name']}({code}) | {v.get('reason', '')}")
            else:
                passed.append(candidate)
                print(f"  [PASS]   {candidate['name']}({code}) | {v.get('reason', '')} (信頼度: {v.get('confidence', 0):.0%})")

        # 検証ログを保存
        _save_validation_log(candidates, verdicts, elapsed)

        if rejected:
            print(f"  [Validation] {len(passed)}件PASS / {len(rejected)}件REJECT")
        else:
            print(f"  [Validation] 全{len(passed)}件PASS")

        if dry_run:
            return candidates  # dry_runでは全候補を返す

        return passed

    except subprocess.TimeoutExpired:
        logger.warning(f"claude -p timeout ({CLAUDE_TIMEOUT}s)、全候補をブロック（fail-closed）")
        print(f"  [Validation] タイムアウト、全候補をブロック（fail-closed）")
        _fail_safe_reject(candidates, f"タイムアウト({CLAUDE_TIMEOUT}s)")
        if dry_run:
            return candidates
        return []
    except FileNotFoundError:
        logger.warning("claude CLI が見つかりません、全候補をブロック（fail-closed）")
        print(f"  [Validation] claude CLI未検出、全候補をブロック（fail-closed）")
        _fail_safe_reject(candidates, "claude CLI未検出")
        if dry_run:
            return candidates
        return []
    except Exception as e:
        logger.warning(f"検証中にエラー: {e}、全候補をブロック（fail-closed）")
        print(f"  [Validation] エラー({e})、全候補をブロック（fail-closed）")
        _fail_safe_reject(candidates, f"例外: {e}")
        if dry_run:
            return candidates
        return []


def _parse_verdicts(text: str) -> list:
    """LLM応答からJSON配列を抽出する。"""
    import re

    # ```json ... ``` ブロックを探す
    json_match = re.search(r'```json\s*\n?(.*?)```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # [ ... ] を直接探す
    bracket_match = re.search(r'\[.*\]', text, re.DOTALL)
    if bracket_match:
        try:
            return json.loads(bracket_match.group(0))
        except json.JSONDecodeError:
            pass

    return []


def _save_validation_log(candidates: list, verdicts: list, elapsed: float):
    """検証結果をログファイルに追記する。"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "candidate_count": len(candidates),
        "verdicts": verdicts,
        "candidates": [
            {
                "code": c["code"],
                "name": c["name"],
                "market": c["market"],
                "strategy": c["strategy"],
                "price": c["price"],
                "signal": c.get("signal"),
                "fundamental_score": c.get("fundamental", {}).get("score"),
            }
            for c in candidates
        ],
    }

    log = []
    if os.path.exists(VALIDATION_LOG_FILE):
        try:
            with open(VALIDATION_LOG_FILE, "r") as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []

    log.append(entry)

    # 直近500件のみ保持
    if len(log) > 500:
        log = log[-500:]

    with open(VALIDATION_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)

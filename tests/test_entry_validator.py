"""
test_entry_validator.py — Dexter式エントリー検証レイヤーのテスト

テスト観点:
  1. _parse_verdicts: JSON直接パース
  2. _parse_verdicts: コードブロック内JSON
  3. _parse_verdicts: パース不能テキスト → 空リスト
  4. _parse_verdicts: 複数候補のパース
  5. _build_validation_prompt: プロンプト生成（必須要素の存在確認）
  6. validate_entries: 空候補 → 空リスト返却
  7. validate_entries: claude CLI未検出時 → 全候補PASS（フェイルセーフ）
  8. validate_entries: タイムアウト時 → 全候補PASS（フェイルセーフ）
  9. _save_validation_log: ログ書き込み・読み出し
  10. validate_entries: REJECT判定の候補がフィルタされること
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

AUTO_TRADE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AUTO_TRADE_DIR)

from entry_validator import (
    _parse_verdicts,
    _build_validation_prompt,
    _save_validation_log,
    validate_entries,
    VALIDATION_LOG_FILE,
)


# --- テスト用データ ---

DUMMY_CANDIDATES = [
    {
        "code": "8058.T", "name": "三菱商事", "market": "jp",
        "price": 5706.0, "signal": 1, "strategy": "volscale_sma",
        "fundamental": {"score": 1.0, "reason": "VolScale: ファンダフィルター不適用"},
    },
    {
        "code": "BTC-JPY", "name": "ビットコイン", "market": "btc",
        "price": 14500000, "signal": 1, "strategy": "vol_div",
        "fundamental": {"score": 0.3, "reason": "半減期後上昇期"},
    },
]

DUMMY_PORTFOLIO = {
    "cash_jpy": 235836,
    "positions": [{"code": "AAPL", "market": "us"}],
    "initial_capital_jpy": 300000,
    "total_realized_pnl": -11039,
}


# --- 1. _parse_verdicts: JSON直接パース ---

def test_parse_verdicts_direct_json():
    text = '[{"code": "8058.T", "verdict": "PASS", "confidence": 0.8, "reason": "OK"}]'
    result = _parse_verdicts(text)
    assert len(result) == 1
    assert result[0]["code"] == "8058.T"
    assert result[0]["verdict"] == "PASS"


# --- 2. _parse_verdicts: コードブロック内JSON ---

def test_parse_verdicts_code_block():
    text = '''説明文
```json
[{"code": "8058.T", "verdict": "REJECT", "confidence": 0.9, "reason": "集中リスク"}]
```
'''
    result = _parse_verdicts(text)
    assert len(result) == 1
    assert result[0]["verdict"] == "REJECT"


# --- 3. _parse_verdicts: パース不能 → 空リスト ---

def test_parse_verdicts_invalid():
    assert _parse_verdicts("関係ないテキスト") == []
    assert _parse_verdicts("") == []


# --- 4. _parse_verdicts: 複数候補 ---

def test_parse_verdicts_multiple():
    text = '''```json
[
  {"code": "8058.T", "verdict": "PASS", "confidence": 0.8, "reason": "OK"},
  {"code": "BTC-JPY", "verdict": "REJECT", "confidence": 0.7, "reason": "ボラ高"}
]
```'''
    result = _parse_verdicts(text)
    assert len(result) == 2
    assert result[0]["verdict"] == "PASS"
    assert result[1]["verdict"] == "REJECT"


# --- 5. _build_validation_prompt: 必須要素の存在 ---

def test_build_prompt_contains_essentials():
    summary = {"cash_jpy": 200000, "position_count": 3, "total_value": 280000, "realized_pnl": -5000}
    prompt = _build_validation_prompt(DUMMY_CANDIDATES, summary, "NORMAL")
    assert "三菱商事" in prompt
    assert "8058.T" in prompt
    assert "BTC-JPY" in prompt
    assert "NORMAL" in prompt
    assert "200,000" in prompt
    assert "PASS" in prompt
    assert "REJECT" in prompt


# --- 6. validate_entries: 空候補 → 空リスト ---

def test_validate_empty_candidates():
    result = validate_entries([], DUMMY_PORTFOLIO)
    assert result == []


# --- 7. validate_entries: claude CLI未検出 → 全候補ブロック（fail-closed） ---

def test_validate_claude_not_found():
    with patch("entry_validator.subprocess.run", side_effect=FileNotFoundError("claude not found")):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO)
    assert len(result) == 0  # fail-closed: 全候補ブロック


def test_validate_claude_not_found_dry_run():
    """dry_runモードではfail-closedでも全候補を返す"""
    with patch("entry_validator.subprocess.run", side_effect=FileNotFoundError("claude not found")):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO, dry_run=True)
    assert len(result) == 2  # dry_runでは全候補が返る


# --- 8. validate_entries: タイムアウト → 全候補ブロック（fail-closed） ---

def test_validate_timeout():
    import subprocess
    with patch("entry_validator.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO)
    assert len(result) == 0  # fail-closed: 全候補ブロック


# --- 9. _save_validation_log: ログ書き込み ---

def test_save_validation_log(tmp_path):
    log_file = tmp_path / "validation_log.json"
    with patch("entry_validator.VALIDATION_LOG_FILE", str(log_file)):
        _save_validation_log(
            DUMMY_CANDIDATES,
            [{"code": "8058.T", "verdict": "PASS", "confidence": 0.8, "reason": "OK"}],
            elapsed=2.5,
        )
    assert log_file.exists()
    data = json.loads(log_file.read_text())
    assert len(data) == 1
    assert data[0]["candidate_count"] == 2
    assert data[0]["elapsed_sec"] == 2.5


# --- 10. validate_entries: REJECT判定のフィルタ ---

def test_validate_rejects_filtered():
    mock_stdout = json.dumps({
        "result": json.dumps([
            {"code": "8058.T", "verdict": "PASS", "confidence": 0.8, "reason": "OK"},
            {"code": "BTC-JPY", "verdict": "REJECT", "confidence": 0.9, "reason": "ボラティリティ過大"},
        ])
    })
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = mock_stdout
    mock_result.stderr = ""

    with patch("entry_validator.subprocess.run", return_value=mock_result), \
         patch("entry_validator.VALIDATION_LOG_FILE", "/dev/null"):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO)

    assert len(result) == 1
    assert result[0]["code"] == "8058.T"
    assert result[0]["validation"]["verdict"] == "PASS"


# --- 11. validate_entries: verdict_map欠損 → REJECT（fail-closed） ---

def test_validate_verdict_map_missing_code():
    """LLMが一部の候補しか返さなかった場合、未返却候補はREJECT"""
    mock_stdout = json.dumps({
        "result": json.dumps([
            {"code": "8058.T", "verdict": "PASS", "confidence": 0.8, "reason": "OK"},
            # BTC-JPY が欠損
        ])
    })
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = mock_stdout
    mock_result.stderr = ""

    with patch("entry_validator.subprocess.run", return_value=mock_result), \
         patch("entry_validator.VALIDATION_LOG_FILE", "/dev/null"):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO)

    assert len(result) == 1
    assert result[0]["code"] == "8058.T"


# --- 12. _fail_safe_reject: shadow logが書き込まれる ---

def test_fail_safe_reject_writes_shadow_log(tmp_path):
    from entry_validator import _fail_safe_reject
    shadow_log = tmp_path / "validation_shadow_log.json"
    with patch("entry_validator.BASE_DIR", str(tmp_path)):
        _fail_safe_reject(DUMMY_CANDIDATES, "テスト用reject")
    assert shadow_log.exists()
    data = json.loads(shadow_log.read_text())
    assert len(data) == 1
    assert data[0]["reason"] == "テスト用reject"
    assert len(data[0]["rejected_candidates"]) == 2


# --- 13. validate_entries: LLM失敗(returncode!=0) → fail-closed ---

def test_validate_llm_failure_fail_closed():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "error"

    with patch("entry_validator.subprocess.run", return_value=mock_result):
        result = validate_entries(DUMMY_CANDIDATES, DUMMY_PORTFOLIO)
    assert len(result) == 0

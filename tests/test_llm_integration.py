"""
test_llm_integration.py — unified_screener.pyのLLMシグナル統合機能テスト

テスト観点:
  1. ファイルが存在しない場合 → 空リスト返却（既存処理に影響なし）
  2. ファイルが空の場合 → 空リスト返却
  3. JSONパースエラー → 空リスト返却
  4. rejected=True のシグナル → 空リスト返却
  5. 有効期限切れ → 空リスト返却
  6. 正常なシグナル → marketsリストを返却
  7. summaryが無い場合 → resultsから自動再計算
  8. 不正な構造（markets が辞書型等） → 空リスト返却
  9. market/resultsキーが欠落したエントリ → スキップ
  10. 有効期限チェック無効化（max_age_seconds=0）
  11. print_market_results がLLM市場でKeyError起こさないこと
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

# auto-tradeをパスに追加
AUTO_TRADE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AUTO_TRADE_DIR)

from unified_screener import load_llm_signals, print_market_results


# ==============================================================
# ヘルパー: テスト用シグナルJSON生成
# ==============================================================

def _make_signal(
    market="jp",
    market_name="日本株(LLM戦略)",
    strategy="llm_test",
    results=None,
    timestamp=None,
    rejected=False,
    reject_reason=None,
    include_summary=True,
):
    """strategy_bridge.py互換のシグナルJSONを生成する。"""
    if results is None:
        results = [
            {
                "code": "7203.T",
                "name": "トヨタ自動車",
                "signal": "BUY",
                "price": 2500.0,
                "change_pct": 1.2,
                "score": 75.0,
                "reason": "LLM戦略 weight=0.75 (Sharpe=1.200)",
                "data_date": "2026-03-20",
                "error": None,
            },
            {
                "code": "6758.T",
                "name": "ソニー",
                "signal": "SELL",
                "price": 3100.0,
                "change_pct": -0.5,
                "score": 30.0,
                "reason": "LLM戦略 weight=-0.30 (Sharpe=1.200)",
                "data_date": "2026-03-20",
                "error": None,
            },
            {
                "code": "9984.T",
                "name": "ソフトバンクG",
                "signal": "NEUTRAL",
                "price": 8500.0,
                "change_pct": 0.0,
                "score": 1.0,
                "reason": "LLM戦略 weight=0.01 (Sharpe=1.200)",
                "data_date": "2026-03-20",
                "error": None,
            },
        ]

    if timestamp is None:
        timestamp = datetime.now().isoformat()

    data = {
        "timestamp": timestamp,
        "source": "llm-trade-bot/strategy_bridge",
        "markets": [
            {
                "market": market,
                "market_name": market_name,
                "strategy": strategy,
                "results": results,
            }
        ],
    }

    if include_summary:
        data["markets"][0]["summary"] = {
            "total": len(results),
            "buy": sum(1 for r in results if r.get("signal") == "BUY"),
            "sell": sum(1 for r in results if r.get("signal") == "SELL"),
            "neutral": sum(1 for r in results if r.get("signal") == "NEUTRAL"),
            "error": 0,
        }

    if rejected:
        data["rejected"] = True
        if reject_reason:
            data["reject_reason"] = reject_reason

    return data


def _write_signal(tmpdir, data, filename="signals.json"):
    """シグナルJSONをファイルに書き出してパスを返す。"""
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


# ==============================================================
# テスト本体
# ==============================================================

class TestLoadLlmSignals:
    """load_llm_signals関数の全分岐テスト。"""

    def test_file_not_exists(self):
        """ファイルが存在しない場合 → 空リスト"""
        result = load_llm_signals("/tmp/nonexistent_llm_signals_xyz.json")
        assert result == []

    def test_file_empty(self, tmp_path):
        """空ファイル → 空リスト"""
        path = tmp_path / "signals.json"
        path.write_text("")
        result = load_llm_signals(str(path))
        assert result == []

    def test_file_whitespace_only(self, tmp_path):
        """空白のみのファイル → 空リスト"""
        path = tmp_path / "signals.json"
        path.write_text("   \n  ")
        result = load_llm_signals(str(path))
        assert result == []

    def test_invalid_json(self, tmp_path):
        """不正なJSON → 空リスト"""
        path = tmp_path / "signals.json"
        path.write_text("{invalid json!!!")
        result = load_llm_signals(str(path))
        assert result == []

    def test_rejected_signal(self, tmp_path):
        """rejected=Trueのシグナル → 空リスト"""
        data = _make_signal(rejected=True, reject_reason="Sharpe 0.200 < 0.5")
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert result == []

    def test_expired_signal(self, tmp_path):
        """25時間前のシグナル（24h制限） → 空リスト"""
        old_time = (datetime.now() - timedelta(hours=25)).isoformat()
        data = _make_signal(timestamp=old_time)
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path, max_age_seconds=86400)
        assert result == []

    def test_valid_signal(self, tmp_path):
        """正常なシグナル → marketsリストを返す"""
        data = _make_signal()
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1
        assert result[0]["market"] == "jp"
        assert result[0]["summary"]["buy"] == 1
        assert result[0]["summary"]["sell"] == 1
        assert result[0]["summary"]["neutral"] == 1
        assert len(result[0]["results"]) == 3

    def test_valid_signal_result_contents(self, tmp_path):
        """正常なシグナルの中身が正しい形式か"""
        data = _make_signal()
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        buy_result = [r for r in result[0]["results"] if r["signal"] == "BUY"][0]
        assert buy_result["code"] == "7203.T"
        assert buy_result["score"] == 75.0
        assert "LLM戦略" in buy_result["reason"]

    def test_no_summary_auto_calculate(self, tmp_path):
        """summaryが無い場合 → resultsから自動再計算"""
        data = _make_signal(include_summary=False)
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1
        summary = result[0]["summary"]
        assert summary["total"] == 3
        assert summary["buy"] == 1
        assert summary["sell"] == 1
        assert summary["neutral"] == 1
        assert summary["error"] == 0

    def test_markets_not_list(self, tmp_path):
        """marketsが辞書型の場合 → 空リスト"""
        data = _make_signal()
        data["markets"] = {"invalid": "structure"}
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert result == []

    def test_market_entry_missing_key(self, tmp_path):
        """market/resultsキーが欠落したエントリ → スキップ"""
        data = _make_signal()
        # 不正なエントリを追加
        data["markets"].append({"strategy": "broken"})  # market, resultsが無い
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        # 正常な1件だけ残る
        assert len(result) == 1

    def test_max_age_zero_disables_check(self, tmp_path):
        """max_age_seconds=0 → 期限チェック無効"""
        old_time = (datetime.now() - timedelta(days=30)).isoformat()
        data = _make_signal(timestamp=old_time)
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path, max_age_seconds=0)
        assert len(result) == 1

    def test_fresh_signal_within_limit(self, tmp_path):
        """1時間前のシグナル（24h以内） → 正常読み込み"""
        recent_time = (datetime.now() - timedelta(hours=1)).isoformat()
        data = _make_signal(timestamp=recent_time)
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1

    def test_empty_results(self, tmp_path):
        """results=[]のシグナル → marketsリストは返す（空市場）"""
        data = _make_signal(results=[])
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1
        assert result[0]["results"] == []

    def test_multiple_markets(self, tmp_path):
        """複数市場のシグナル → 全て返す"""
        data = _make_signal()
        us_market = {
            "market": "us",
            "market_name": "米国株(LLM戦略)",
            "strategy": "llm_test_us",
            "results": [
                {
                    "code": "AAPL",
                    "name": "Apple",
                    "signal": "BUY",
                    "price": 180.0,
                    "change_pct": 2.0,
                    "score": 85.0,
                    "reason": "LLM戦略",
                    "data_date": "2026-03-20",
                    "error": None,
                }
            ],
            "summary": {"total": 1, "buy": 1, "sell": 0, "neutral": 0, "error": 0},
        }
        data["markets"].append(us_market)
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 2

    def test_invalid_timestamp_format(self, tmp_path):
        """不正なタイムスタンプ → 期限チェックをスキップして正常読み込み"""
        data = _make_signal(timestamp="not-a-date")
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1

    def test_no_timestamp(self, tmp_path):
        """タイムスタンプが無い場合 → 期限チェックスキップ"""
        data = _make_signal()
        del data["timestamp"]
        path = _write_signal(str(tmp_path), data)
        result = load_llm_signals(path)
        assert len(result) == 1


class TestPrintMarketResultsLlm:
    """print_market_resultsがLLM市場データでKeyErrorにならないことを確認。"""

    def test_llm_market_no_crash(self, capsys):
        """MARKET_CONFIGに無いmarket keyでもKeyErrorにならない"""
        market_data = {
            "market": "jp",  # MARKET_CONFIGにはjpが存在するが念のため
            "market_name": "日本株(LLM戦略)",
            "strategy": "llm_test",
            "results": [],
            "summary": {"total": 0, "buy": 0, "sell": 0, "neutral": 0, "error": 0},
        }
        # LLMのmarketキーはMARKET_CONFIGに無い可能性がある
        # jp_llmのようなキーでテスト
        market_data_llm = {
            "market": "jp_llm",
            "market_name": "日本株(LLM戦略)",
            "strategy": "llm_strategy",
            "results": [
                {
                    "code": "7203.T",
                    "name": "トヨタ",
                    "signal": "BUY",
                    "price": 2500.0,
                    "change_pct": 1.0,
                    "score": 75.0,
                    "reason": "LLM",
                    "data_date": "2026-03-20",
                    "error": None,
                },
            ],
            "summary": {"total": 1, "buy": 1, "sell": 0, "neutral": 0, "error": 0},
        }
        # KeyErrorが出なければ成功
        print_market_results(market_data_llm)
        captured = capsys.readouterr()
        assert "日本株(LLM戦略)" in captured.out
        assert "llm_strategy" in captured.out

    def test_existing_market_still_works(self, capsys):
        """既存のMARKET_CONFIGに存在するmarket keyも引き続き動作"""
        market_data = {
            "market": "btc",
            "market_name": "仮想通貨",
            "strategy": "vol_div",
            "results": [],
            "summary": {"total": 0, "buy": 0, "sell": 0, "neutral": 0, "error": 0},
        }
        print_market_results(market_data)
        captured = capsys.readouterr()
        assert "仮想通貨" in captured.out
        assert "VolumeDivergenceStrategy" in captured.out

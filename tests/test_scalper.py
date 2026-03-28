"""
test_scalper.py — スキャル専用トレーダーのテスト

テスト観点:
  1. calc_atr: 正常なATR計算
  2. calc_atr: データ不足 → None
  3. get_scalp_sltp: ATRからSL/TP計算（キャップ内）
  4. get_scalp_sltp: ATR=None → フォールバック値
  5. get_scalp_sltp: RR比が2:1であること
  6. create_initial_portfolio: 初期値の確認
  7. is_in_cooldown: クールダウンなし → False
  8. execute_entry: dry_run → ポートフォリオ変更なし
  9. execute_entry: 通常実行 → ポジション追加
  10. execute_exit: 利確決済 → PnL反映
  11. check_exit: TP到達検出（モック）
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

AUTO_TRADE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AUTO_TRADE_DIR)

from scalper import (
    calc_atr,
    get_scalp_sltp,
    create_initial_portfolio,
    is_in_cooldown,
    execute_entry,
    execute_exit,
    SCALP_SYMBOLS,
    INITIAL_CAPITAL_JPY,
    SL_CAP_PCT,
    TP_CAP_PCT,
    SL_FLOOR_PCT,
    TP_FLOOR_PCT,
)


def _make_ohlcv(n: int = 30, base: float = 10000.0, volatility: float = 50.0) -> pd.DataFrame:
    """テスト用OHLCVデータ生成。"""
    np.random.seed(42)
    closes = [base + np.random.normal(0, volatility) for _ in range(n)]
    return pd.DataFrame({
        "open": [c - np.random.uniform(0, 20) for c in closes],
        "high": [c + np.random.uniform(10, 30) for c in closes],
        "low": [c - np.random.uniform(10, 30) for c in closes],
        "close": closes,
        "volume": [1000] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="5min"))


def _make_portfolio(**kwargs) -> dict:
    defaults = {
        "initial_capital_jpy": INITIAL_CAPITAL_JPY,
        "cash_jpy": INITIAL_CAPITAL_JPY,
        "leverage": 10,
        "positions": [],
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
    }
    defaults.update(kwargs)
    return defaults


# --- 1. calc_atr: 正常計算 ---
def test_calc_atr_normal():
    data = _make_ohlcv(30)
    atr = calc_atr(data)
    assert atr is not None
    assert 0 < atr < 0.1  # パーセンテージで10%未満


# --- 2. calc_atr: データ不足 ---
def test_calc_atr_insufficient():
    data = _make_ohlcv(5)
    atr = calc_atr(data)
    assert atr is None


# --- 3. get_scalp_sltp: キャップ内 ---
def test_sltp_within_caps():
    sltp = get_scalp_sltp(0.005)  # ATR 0.5%
    assert SL_FLOOR_PCT <= abs(sltp["sl_pct"]) <= SL_CAP_PCT
    assert TP_FLOOR_PCT <= sltp["tp_pct"] <= TP_CAP_PCT


# --- 4. get_scalp_sltp: ATR=None → フォールバック ---
def test_sltp_none_atr():
    sltp = get_scalp_sltp(None)
    assert sltp["sl_pct"] < 0
    assert sltp["tp_pct"] > 0


# --- 5. get_scalp_sltp: RR比 2:1 ---
def test_sltp_rr_ratio():
    sltp = get_scalp_sltp(0.005)
    rr = sltp["tp_pct"] / abs(sltp["sl_pct"])
    assert 1.8 <= rr <= 2.2  # 2:1 ± 許容誤差


# --- 6. create_initial_portfolio ---
def test_create_initial_portfolio(tmp_path):
    with patch("scalper.SCALP_PORTFOLIO_FILE", str(tmp_path / "test_portfolio.json")):
        p = create_initial_portfolio()
    assert p["cash_jpy"] == INITIAL_CAPITAL_JPY
    assert p["leverage"] == 10
    assert p["positions"] == []
    assert p["total_realized_pnl"] == 0.0


# --- 7. is_in_cooldown: ログなし → False ---
def test_cooldown_no_log():
    with patch("scalper.SCALP_TRADE_LOG_FILE", "/tmp/nonexistent_log.json"):
        assert is_in_cooldown("BTC") is False


# --- 8. execute_entry: dry_run ---
def test_entry_dry_run():
    portfolio = _make_portfolio()
    original_cash = portfolio["cash_jpy"]
    sltp = {"sl_pct": -0.008, "tp_pct": 0.016}
    result = execute_entry(portfolio, "BTC", "long", 14000000, sltp, dry_run=True)
    assert result["cash_jpy"] == original_cash  # 変更なし
    assert len(result["positions"]) == 0


# --- 9. execute_entry: 通常実行 ---
def test_entry_normal():
    portfolio = _make_portfolio()
    sltp = {"sl_pct": -0.008, "tp_pct": 0.016}
    with patch("scalper.append_trade_log"):
        result = execute_entry(portfolio, "BTC", "long", 14000000, sltp)
    assert len(result["positions"]) == 1
    assert result["positions"][0]["side"] == "long"
    assert result["positions"][0]["symbol_key"] == "BTC"
    assert result["cash_jpy"] < INITIAL_CAPITAL_JPY  # 証拠金引落し


# --- 10. execute_exit: 利確決済 ---
def test_exit_take_profit():
    portfolio = _make_portfolio()
    pos = {
        "symbol_key": "BTC",
        "code": "BTC-JPY",
        "name": "ビットコイン",
        "side": "long",
        "shares": 0.001,
        "entry_price": 14000000,
        "entry_date": "2026-03-29T00:00:00",
        "sl_pct": -0.008,
        "tp_pct": 0.016,
        "invest_jpy": 10000,
        "margin_jpy": 1000,
        "high_since_entry": 14000000,
    }
    portfolio["positions"].append(pos)
    portfolio["cash_jpy"] -= pos["margin_jpy"]

    exit_info = {
        "pos": pos,
        "current_price": 14224000,  # +1.6%
        "pnl_pct": 0.016,
        "reason": "TAKE_PROFIT",
    }

    with patch("scalper.append_trade_log"):
        result = execute_exit(portfolio, exit_info)

    assert len(result["positions"]) == 0
    assert result["total_trades"] == 1
    assert result["wins"] == 1
    assert result["total_realized_pnl"] > 0


# --- 11. check_exit: モックでTP到達検出 ---
def test_check_exit_detects_tp():
    portfolio = _make_portfolio()
    pos = {
        "symbol_key": "BTC",
        "code": "BTC-JPY",
        "name": "ビットコイン",
        "side": "long",
        "shares": 0.001,
        "entry_price": 14000000,
        "entry_date": "2026-03-29T00:00:00",
        "sl_pct": -0.008,
        "tp_pct": 0.016,
        "invest_jpy": 10000,
        "margin_jpy": 1000,
        "high_since_entry": 14000000,
    }
    portfolio["positions"].append(pos)

    # TP到達する価格（+2%）のOHLCVを返すモック
    mock_df = pd.DataFrame({
        "open": [14200000], "high": [14300000],
        "low": [14100000], "close": [14280000],  # +2% > tp 1.6%
        "volume": [100],
    }, index=pd.date_range("2026-03-29", periods=1, freq="5min"))

    with patch("scalper.fetch_ohlcv_ccxt", return_value=mock_df):
        from scalper import check_exit
        exits = check_exit(portfolio)

    assert len(exits) == 1
    assert exits[0]["reason"] == "TAKE_PROFIT"


# --- 12. get_scalp_sltp: RR比がキャップ適用後も2:1を維持 ---
def test_sltp_rr_ratio_after_cap():
    """ATRが大きくてSLがキャップに引っかかる場合もRR比2:1を維持する。"""
    sltp = get_scalp_sltp(0.02)  # ATR 2% → SL_raw=3% > CAP=1.5%
    rr = sltp["tp_pct"] / abs(sltp["sl_pct"])
    assert 1.8 <= rr <= 2.2, f"RR比が崩れている: {rr:.2f}"


# --- 13. get_scalp_sltp: ATRが極小の場合もフロア適用+RR維持 ---
def test_sltp_rr_ratio_small_atr():
    """ATRが極小でフロアに引っかかる場合のRR比。"""
    sltp = get_scalp_sltp(0.001)  # ATR 0.1% → SL_raw=0.15% < FLOOR=0.3%
    assert abs(sltp["sl_pct"]) >= SL_FLOOR_PCT
    assert sltp["tp_pct"] >= TP_FLOOR_PCT
    rr = sltp["tp_pct"] / abs(sltp["sl_pct"])
    assert 1.8 <= rr <= 2.2, f"RR比が崩れている: {rr:.2f}"


# --- 14. execute_entry: 現金不足 → スキップ ---
def test_entry_insufficient_cash():
    portfolio = _make_portfolio(cash_jpy=50)  # 50円しかない
    sltp = {"sl_pct": -0.008, "tp_pct": 0.016}
    result = execute_entry(portfolio, "BTC", "long", 14000000, sltp)
    assert len(result["positions"]) == 0  # エントリーされない


# --- 15. execute_exit: ショート損切り決済 ---
def test_exit_short_stop_loss():
    portfolio = _make_portfolio()
    pos = {
        "symbol_key": "ETH",
        "code": "ETH-JPY",
        "name": "イーサリアム",
        "side": "short",
        "shares": 0.01,
        "entry_price": 500000,
        "entry_date": "2026-03-29T00:00:00",
        "sl_pct": -0.008,
        "tp_pct": 0.016,
        "invest_jpy": 10000,
        "margin_jpy": 1000,
        "low_since_entry": 500000,
        "high_since_entry": 500000,
    }
    portfolio["positions"].append(pos)
    portfolio["cash_jpy"] -= pos["margin_jpy"]

    exit_info = {
        "pos": pos,
        "current_price": 504000,  # ショートで価格上昇 = 損失
        "pnl_pct": -0.008,
        "reason": "STOP_LOSS",
    }

    with patch("scalper.append_trade_log"):
        result = execute_exit(portfolio, exit_info)

    assert len(result["positions"]) == 0
    assert result["total_trades"] == 1
    assert result["losses"] == 1
    assert result["total_realized_pnl"] < 0


# --- 16. execute_exit: JSON復元後のポジション削除が正しく動作 ---
def test_exit_position_removal_after_json_roundtrip():
    """ポジションをJSON経由で復元した後でもexecuteExitが正しくポジションを削除できる。"""
    portfolio = _make_portfolio()
    pos = {
        "symbol_key": "BTC",
        "code": "BTC-JPY",
        "name": "ビットコイン",
        "side": "long",
        "shares": 0.001,
        "entry_price": 14000000,
        "entry_date": "2026-03-29T01:00:00",
        "sl_pct": -0.008,
        "tp_pct": 0.016,
        "invest_jpy": 10000,
        "margin_jpy": 1000,
        "high_since_entry": 14000000,
        "low_since_entry": 14000000,
    }
    portfolio["positions"].append(pos)

    # JSONラウンドトリップ（実運用と同じ状態をシミュレーション）
    portfolio = json.loads(json.dumps(portfolio))
    target_pos = portfolio["positions"][0]

    exit_info = {
        "pos": target_pos,
        "current_price": 14224000,
        "pnl_pct": 0.016,
        "reason": "TAKE_PROFIT",
    }

    with patch("scalper.append_trade_log"):
        result = execute_exit(portfolio, exit_info)

    assert len(result["positions"]) == 0, "JSONラウンドトリップ後にポジション削除が失敗"

"""
test_scalp_trend.py — ScalpTrend戦略のテスト

テスト観点:
  1. detect_trend: 上昇トレンド判定
  2. detect_trend: 下降トレンド判定
  3. detect_trend: レンジ判定
  4. detect_trend: データ不足 → range
  5. generate_signals: 上昇トレンドでロングシグナル
  6. generate_signals: 下降トレンドでショートシグナル
  7. generate_signals: レンジ → シグナルなし
  8. generate_signals: データ不足 → シグナルなし
  9. position_size: 計算が正しいこと
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

AUTO_TRADE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AUTO_TRADE_DIR)

from strategies.scalp_trend import ScalpTrendStrategy


def _make_data(prices: list, base_volume: int = 1000) -> pd.DataFrame:
    """テスト用OHLCVデータを生成。"""
    n = len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices],
        "close": prices,
        "volume": [base_volume] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="5min"))


def _make_uptrend(n: int = 100, start: float = 100.0, step: float = 0.5) -> list:
    """上昇トレンドの価格リスト。"""
    return [start + i * step + np.random.normal(0, 0.1) for i in range(n)]


def _make_downtrend(n: int = 100, start: float = 200.0, step: float = 0.5) -> list:
    """下降トレンドの価格リスト。"""
    return [start - i * step + np.random.normal(0, 0.1) for i in range(n)]


def _make_range(n: int = 100, center: float = 150.0) -> list:
    """レンジ相場の価格リスト（EMA20≈EMA50でclose付近を横ばい）。"""
    np.random.seed(42)
    # 完全フラットに微小ノイズ → EMA20とEMA50がほぼ一致 → range判定
    return [center + np.random.normal(0, 0.01) for _ in range(n)]


@pytest.fixture
def strategy():
    return ScalpTrendStrategy()


# --- 1. detect_trend: 上昇トレンド ---
def test_detect_trend_up(strategy):
    data = _make_data(_make_uptrend(100))
    trend = strategy.detect_trend(data)
    assert trend == "up"


# --- 2. detect_trend: 下降トレンド ---
def test_detect_trend_down(strategy):
    data = _make_data(_make_downtrend(100))
    trend = strategy.detect_trend(data)
    assert trend == "down"


# --- 3. detect_trend: レンジ ---
def test_detect_trend_range(strategy):
    data = _make_data(_make_range(100))
    trend = strategy.detect_trend(data)
    assert trend == "range"


# --- 4. detect_trend: データ不足 ---
def test_detect_trend_insufficient_data(strategy):
    data = _make_data([100, 101, 102])
    trend = strategy.detect_trend(data)
    assert trend == "range"


# --- 5. generate_signals: 上昇トレンドでロング ---
def test_signals_uptrend_long(strategy):
    data = _make_data(_make_uptrend(100))
    signals = strategy.generate_signals(data, trend="up")
    assert (signals == 1).any()
    assert not (signals == -1).any()


# --- 6. generate_signals: 下降トレンドでショート ---
def test_signals_downtrend_short(strategy):
    data = _make_data(_make_downtrend(100))
    signals = strategy.generate_signals(data, trend="down")
    assert (signals == -1).any()
    assert not (signals == 1).any()


# --- 7. generate_signals: レンジ → シグナルなし ---
def test_signals_range_no_signal(strategy):
    data = _make_data(_make_range(100))
    signals = strategy.generate_signals(data, trend="range")
    assert (signals == 0).all()


# --- 8. generate_signals: データ不足 ---
def test_signals_insufficient_data(strategy):
    data = _make_data([100, 101, 102])
    signals = strategy.generate_signals(data, trend="up")
    assert (signals == 0).all()


# --- 9. position_size ---
def test_position_size(strategy):
    size = strategy.position_size(1, 100000, 5000)
    # risk_per_trade=0.02 → 100000 * 0.02 / 5000 = 0.4
    assert abs(size - 0.4) < 0.001

"""
モメンタム + マイクロプルバック戦略（ロス・キャメロン手法）

出来高急増で強い上昇トレンドを見せる銘柄に対し、
短期EMAへの押し目（マイクロプルバック）を狙ってエントリーする。

■ エントリー条件（全て合致）：
  1. 価格 > EMA9, EMA20, EMA200（上昇トレンド確認）
  2. 価格 > VWAP（出来高加重の上側）
  3. MACDがゴールデンクロス状態（MACDヒストグラム > 0）
  4. 上昇時に出来高増、調整時に出来高減
  5. EMA9 or EMA20にタッチして反発（マイクロプルバック）
  6. 押し目の陰線高値を突破した瞬間にエントリー

■ 決済条件：
  - 損切り: 押し目の安値を割る or EMA20を割る
  - 利確: MACDデッドクロス or 200EMAタッチ（下降時）
  - トレーリングストップ: EMA9に沿って損切りラインを引き上げ
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta
from plugins.indicators.feature_engine import FeatureEngine


class MomentumPullbackStrategy(BaseStrategy):
    """モメンタム + マイクロプルバック戦略"""

    def __init__(self, params=None):
        meta = StrategyMeta(
            name="Momentum_Pullback",
            market="crypto",
            version="1.0.0",
            description="Ross Cameron momentum + micro pullback to EMA strategy",
            tags=["momentum", "pullback", "ema", "vwap", "macd", "volume"],
        )
        default_params = {
            "ema_fast": 9,
            "ema_mid": 20,
            "ema_slow": 200,
            "pullback_tolerance": 0.002,  # EMAタッチ判定の許容幅（0.2%）
            "vol_increase_lookback": 3,   # 出来高パターン確認の遡り本数
            "risk_per_trade": 0.05,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)
        self._fe = FeatureEngine()

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()

        ema_fast = self.params["ema_fast"]
        ema_mid = self.params["ema_mid"]
        ema_slow = self.params["ema_slow"]
        tolerance = self.params["pullback_tolerance"]
        vol_lookback = self.params["vol_increase_lookback"]

        # 指標を追加
        df = self._fe.add_ema(df, period=ema_fast)
        df = self._fe.add_ema(df, period=ema_mid)
        df = self._fe.add_ema(df, period=ema_slow)
        df = self._fe.add_vwap(df)
        df = self._fe.add_macd(df)

        ema_fast_col = f"ema_{ema_fast}"
        ema_mid_col = f"ema_{ema_mid}"
        ema_slow_col = f"ema_{ema_slow}"

        signals = pd.Series(0, index=df.index)

        # プルバック検出用: 陰線（close < open）の情報を事前計算
        is_bearish = df["close"] < df["open"]
        is_bullish = df["close"] >= df["open"]

        for i in range(ema_slow + vol_lookback + 2, len(df)):
            # 必要なEMAがNaNならスキップ
            if pd.isna(df[ema_slow_col].iloc[i]) or pd.isna(df["vwap"].iloc[i]):
                continue

            close = df["close"].iloc[i]
            ema9 = df[ema_fast_col].iloc[i]
            ema20 = df[ema_mid_col].iloc[i]
            ema200 = df[ema_slow_col].iloc[i]
            vwap = df["vwap"].iloc[i]
            macd_hist = df["macd_hist"].iloc[i]

            if pd.isna(macd_hist):
                continue

            # --- 買いシグナル ---
            # 条件1: 価格 > EMA9 > EMA20 > EMA200（EMAのパーフェクトオーダー）
            ema_aligned = close > ema9 and ema9 > ema20 and ema20 > ema200

            # 条件2: 価格 > VWAP
            above_vwap = close > vwap

            # 条件3: MACDゴールデンクロス状態（ヒストグラム > 0）
            macd_bullish = macd_hist > 0

            if not (ema_aligned and above_vwap and macd_bullish):
                continue

            # 条件4: 出来高パターン（上昇時に増加、調整時に減少）
            vol_pattern_ok = self._check_volume_pattern(df, i, vol_lookback)
            if not vol_pattern_ok:
                continue

            # 条件5+6: マイクロプルバック検出
            # 直前にEMA9またはEMA20にタッチした陰線があり、
            # 現在のバーがその陰線の高値を突破している
            pullback_entry = self._detect_pullback_breakout(
                df, i, ema_fast_col, ema_mid_col, tolerance,
                is_bearish, is_bullish,
            )

            if pullback_entry:
                signals.iloc[i] = 1

            # --- 売り（決済）シグナル ---
            # ポジション保持中にMACDデッドクロスで決済
            # バックテストエンジンではポジションフリップ方式なので、
            # -1シグナルは反対売買として機能する
            if signals.iloc[i] == 0:  # 買いシグナルが出ていない場合のみ
                # MACDデッドクロス（ヒストグラムが正→負に転換）
                if i >= 1 and not pd.isna(df["macd_hist"].iloc[i - 1]):
                    prev_hist = df["macd_hist"].iloc[i - 1]
                    if prev_hist > 0 and macd_hist <= 0:
                        signals.iloc[i] = -1
                    # EMA20を下回った場合も決済
                    elif close < ema20:
                        signals.iloc[i] = -1

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

    # ----------------------------------------------------------
    # ヘルパーメソッド
    # ----------------------------------------------------------

    @staticmethod
    def _check_volume_pattern(df: pd.DataFrame, idx: int, lookback: int) -> bool:
        """上昇時に出来高増加、調整時に出来高減少のパターンを確認する。

        直近lookback本の中で、陽線時の平均出来高 > 陰線時の平均出来高
        であれば条件を満たす。
        """
        start = max(0, idx - lookback)
        segment = df.iloc[start:idx + 1]

        bullish_bars = segment[segment["close"] >= segment["open"]]
        bearish_bars = segment[segment["close"] < segment["open"]]

        if len(bullish_bars) == 0:
            return False

        bull_vol_avg = bullish_bars["volume"].mean()
        bear_vol_avg = bearish_bars["volume"].mean() if len(bearish_bars) > 0 else 0

        return bull_vol_avg > bear_vol_avg

    @staticmethod
    def _detect_pullback_breakout(
        df: pd.DataFrame,
        idx: int,
        ema_fast_col: str,
        ema_mid_col: str,
        tolerance: float,
        is_bearish: pd.Series,
        is_bullish: pd.Series,
    ) -> bool:
        """マイクロプルバックからのブレイクアウトを検出する。

        直前3本以内に:
        - 陰線がEMA9またはEMA20にタッチ（許容幅tolerance以内）
        - 現在のバーが陽線で、その陰線の高値を突破
        """
        for lookback in range(1, 4):
            j = idx - lookback
            if j < 0:
                break

            # 陰線か確認
            if not is_bearish.iloc[j]:
                continue

            low_j = df["low"].iloc[j]
            high_j = df["high"].iloc[j]
            ema9_j = df[ema_fast_col].iloc[j]
            ema20_j = df[ema_mid_col].iloc[j]

            if pd.isna(ema9_j) or pd.isna(ema20_j):
                continue

            # EMA9 or EMA20にタッチしたか（安値がEMA付近まで下がった）
            touched_ema9 = abs(low_j - ema9_j) / ema9_j <= tolerance
            touched_ema20 = abs(low_j - ema20_j) / ema20_j <= tolerance

            if not (touched_ema9 or touched_ema20):
                continue

            # 現在のバーが陽線で、陰線の高値を突破
            if is_bullish.iloc[idx] and df["close"].iloc[idx] > high_j:
                return True

        return False

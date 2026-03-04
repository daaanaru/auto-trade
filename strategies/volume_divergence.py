"""
出来高ダイバージェンス戦略（MFI + 200EMA + ボリュームオシレーター）

YouTube「心眼のチャート研究所」の出来高戦略をベースに実装。

■ 買いエントリー条件（3つ全て）：
  1. MFI強気ダイバージェンス: 価格の安値が切り下がり、MFIの安値は切り上がり
  2. 価格が200EMAの上（上昇トレンド確認）
  3. ボリュームオシレーターが上昇中

■ 売りエントリー条件（3つ全て）：
  1. MFI弱気ダイバージェンス: 価格の高値が切り上がり、MFIの高値は切り下がり
  2. 価格が200EMAの下（下降トレンド確認）
  3. ボリュームオシレーターが上昇中
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta
from plugins.indicators.feature_engine import FeatureEngine


class VolumeDivergenceStrategy(BaseStrategy):
    """出来高ダイバージェンス戦略（MFI + 200EMA + VO）"""

    def __init__(self, params=None):
        meta = StrategyMeta(
            name="Volume_Divergence",
            market="crypto",
            version="1.0.0",
            description="MFI divergence + 200EMA trend filter + Volume Oscillator",
            tags=["volume", "divergence", "mfi", "ema", "trend_following"],
        )
        default_params = {
            "mfi_period": 14,
            "ema_period": 200,
            "vo_short": 5,
            "vo_long": 10,
            "swing_lookback": 5,       # スイングポイント検出の前後N本
            "divergence_window": 50,   # ダイバージェンス検出の探索範囲
            "risk_per_trade": 0.05,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)
        self._fe = FeatureEngine()

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()

        # 指標を追加
        df = self._fe.add_mfi(df, period=self.params["mfi_period"])
        df = self._fe.add_ema(df, period=self.params["ema_period"])
        df = self._fe.add_volume_oscillator(
            df, short=self.params["vo_short"], long=self.params["vo_long"]
        )

        mfi_col = f"mfi_{self.params['mfi_period']}"
        ema_col = f"ema_{self.params['ema_period']}"
        lookback = self.params["swing_lookback"]
        div_window = self.params["divergence_window"]

        # スイングハイ・スイングローを検出
        swing_lows = self._detect_swing_lows(df["low"], lookback)
        swing_highs = self._detect_swing_highs(df["high"], lookback)

        signals = pd.Series(0, index=df.index)

        for i in range(div_window + lookback, len(df)):
            # 条件2: トレンドフィルター
            price_above_ema = df["close"].iloc[i] > df[ema_col].iloc[i]
            price_below_ema = df["close"].iloc[i] < df[ema_col].iloc[i]

            # EMAがNaNならスキップ（200EMA計算に200本必要）
            if pd.isna(df[ema_col].iloc[i]):
                continue

            # 条件3: ボリュームオシレーター上昇中
            vo_rising = (
                not pd.isna(df["vol_osc"].iloc[i])
                and not pd.isna(df["vol_osc"].iloc[i - 1])
                and df["vol_osc"].iloc[i] > df["vol_osc"].iloc[i - 1]
            )

            if not vo_rising:
                continue

            # 条件1: ダイバージェンス検出
            # --- 強気ダイバージェンス（買い）---
            if price_above_ema:
                if self._bullish_divergence(
                    df["low"], df[mfi_col], swing_lows, i, div_window
                ):
                    signals.iloc[i] = 1

            # --- 弱気ダイバージェンス（売り）---
            if price_below_ema:
                if self._bearish_divergence(
                    df["high"], df[mfi_col], swing_highs, i, div_window
                ):
                    signals.iloc[i] = -1

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

    # ----------------------------------------------------------
    # スイングポイント検出
    # ----------------------------------------------------------

    @staticmethod
    def _detect_swing_lows(series: pd.Series, lookback: int) -> pd.Series:
        """スイングロー（前後N本より安い底値）を検出する。

        Returns:
            bool Series: Trueの位置がスイングロー
        """
        result = pd.Series(False, index=series.index)
        for i in range(lookback, len(series) - lookback):
            window = series.iloc[i - lookback : i + lookback + 1]
            if series.iloc[i] == window.min():
                result.iloc[i] = True
        return result

    @staticmethod
    def _detect_swing_highs(series: pd.Series, lookback: int) -> pd.Series:
        """スイングハイ（前後N本より高い天井値）を検出する。

        Returns:
            bool Series: Trueの位置がスイングハイ
        """
        result = pd.Series(False, index=series.index)
        for i in range(lookback, len(series) - lookback):
            window = series.iloc[i - lookback : i + lookback + 1]
            if series.iloc[i] == window.max():
                result.iloc[i] = True
        return result

    # ----------------------------------------------------------
    # ダイバージェンス検出
    # ----------------------------------------------------------

    @staticmethod
    def _bullish_divergence(
        price_low: pd.Series,
        mfi: pd.Series,
        swing_lows: pd.Series,
        current_idx: int,
        window: int,
    ) -> bool:
        """強気ダイバージェンスを検出する。

        条件: 直近windowの中でスイングローが2つ以上あり、
              価格の安値が切り下がっているのにMFIの安値は切り上がっている。
        """
        start = max(0, current_idx - window)
        region = swing_lows.iloc[start:current_idx + 1]
        swing_indices = [i for i, v in enumerate(region) if v]

        if len(swing_indices) < 2:
            return False

        # 直近2つのスイングローを比較
        idx_prev = start + swing_indices[-2]
        idx_curr = start + swing_indices[-1]

        price_prev = price_low.iloc[idx_prev]
        price_curr = price_low.iloc[idx_curr]
        mfi_prev = mfi.iloc[idx_prev]
        mfi_curr = mfi.iloc[idx_curr]

        if pd.isna(mfi_prev) or pd.isna(mfi_curr):
            return False

        # 価格は切り下がり、MFIは切り上がり → 強気ダイバージェンス
        return price_curr < price_prev and mfi_curr > mfi_prev

    @staticmethod
    def _bearish_divergence(
        price_high: pd.Series,
        mfi: pd.Series,
        swing_highs: pd.Series,
        current_idx: int,
        window: int,
    ) -> bool:
        """弱気ダイバージェンスを検出する。

        条件: 直近windowの中でスイングハイが2つ以上あり、
              価格の高値が切り上がっているのにMFIの高値は切り下がっている。
        """
        start = max(0, current_idx - window)
        region = swing_highs.iloc[start:current_idx + 1]
        swing_indices = [i for i, v in enumerate(region) if v]

        if len(swing_indices) < 2:
            return False

        # 直近2つのスイングハイを比較
        idx_prev = start + swing_indices[-2]
        idx_curr = start + swing_indices[-1]

        price_prev = price_high.iloc[idx_prev]
        price_curr = price_high.iloc[idx_curr]
        mfi_prev = mfi.iloc[idx_prev]
        mfi_curr = mfi.iloc[idx_curr]

        if pd.isna(mfi_prev) or pd.isna(mfi_curr):
            return False

        # 価格は切り上がり、MFIは切り下がり → 弱気ダイバージェンス
        return price_curr > price_prev and mfi_curr < mfi_prev

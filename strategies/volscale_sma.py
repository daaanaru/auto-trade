"""
VolScale SMA戦略: BTC用 動的SMA ロングオンリー

AB3氏の研究に基づく。固定パラメータがWF最適化に勝ち、
フィルター追加は無効という結論を踏まえ、シンプルに実装。

ルール:
  N(t) = clip( base_n * sigma_20(t) / median(sigma_20, 過去ref_w日), 20, 300 )
  ロング: close > SMA(N(t))
  エグジット: close < SMA(N(t))

パラメータ（固定）:
  base_n = 50  (40-60で同等。中央値を採用)
  vol_w  = 20  (ボラ計算窓)
  ref_w  = 180 (ボラ中央値の参照期間)
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta


class VolScaleSMAStrategy(BaseStrategy):
    """動的SMA（VolScale）によるBTCロングオンリー戦略"""

    def __init__(self, params: dict = None):
        meta = StrategyMeta(
            name="VolScale_SMA",
            market="crypto",
            version="1.0.0",
            description="Dynamic SMA period scaled by realized volatility (long-only)",
            tags=["trend", "sma", "volatility", "btc"],
        )
        default_params = {
            "base_n": 50,
            "vol_w": 20,
            "ref_w": 180,
            "n_min": 20,
            "n_max": 300,
            "risk_per_trade": 0.10,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        VolScaleシグナル生成。
        Returns: 1(ロング) / 0(ノーポジション)。SELLは出さない。
        """
        close = data["close"].copy()
        signals = pd.Series(0, index=data.index)

        base_n = self.params["base_n"]
        vol_w = self.params["vol_w"]
        ref_w = self.params["ref_w"]
        n_min = self.params["n_min"]
        n_max = self.params["n_max"]

        # 日次対数リターン
        log_ret = np.log(close / close.shift(1))

        # sigma_20: 直近vol_w日の標準偏差 x sqrt(365) で年率換算
        sigma = log_ret.rolling(window=vol_w).std() * np.sqrt(365)

        # sigma中央値: 過去ref_w日のsigmaの中央値
        sigma_median = sigma.rolling(window=ref_w).median()

        # 動的N(t)
        ratio = sigma / sigma_median
        n_dynamic = (base_n * ratio).clip(lower=n_min, upper=n_max)

        # 動的SMA計算（各日でN(t)が異なるため、ループで計算）
        sma_values = pd.Series(np.nan, index=data.index)
        close_arr = close.values
        n_arr = n_dynamic.values

        for i in range(len(close_arr)):
            n = n_arr[i]
            if np.isnan(n) or i < int(n) - 1:
                continue
            n_int = int(round(n))
            if n_int < 1:
                n_int = 1
            start_idx = max(0, i - n_int + 1)
            sma_values.iloc[i] = close_arr[start_idx : i + 1].mean()

        # シグナル: close > SMA(N(t)) -> 1, else -> 0
        signals[close > sma_values] = 1
        signals[close <= sma_values] = 0
        # NaN期間（ウォームアップ）は0
        signals[sma_values.isna()] = 0

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.10)
        if signal == 1:
            return (portfolio_value * risk_pct) / price
        return 0.0

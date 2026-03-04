"""
ボリンジャーバンド + RSI 複合戦略

買いシグナル: 価格がBB下限を下抜け かつ RSIが30以下（売られすぎ）
売りシグナル: 価格がBB上限を上抜け かつ RSIが70以上（買われすぎ）

単体指標よりダマシを減らすことが狙い。
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta
from plugins.indicators.feature_engine import FeatureEngine


class BBRSIComboStrategy(BaseStrategy):
    """ボリンジャーバンド + RSI 複合逆張り戦略"""

    def __init__(self, params=None):
        meta = StrategyMeta(
            name="BB_RSI_Combo",
            market="crypto",
            version="1.0.0",
            description="Bollinger Bands + RSI combination reversal strategy",
            tags=["mean_reversion", "bollinger", "rsi", "combo"],
        )
        default_params = {
            "bb_period": 20,
            "bb_std": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "risk_per_trade": 0.05,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)
        self._fe = FeatureEngine()

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = self._fe.add_rsi(data.copy(), period=self.params["rsi_period"])
        df = self._fe.add_bollinger_bands(
            df,
            period=self.params["bb_period"],
            std_dev=self.params["bb_std"],
        )

        rsi_col = f"rsi_{self.params['rsi_period']}"
        signals = pd.Series(0, index=df.index)

        # 買い: 価格がBB下限以下 かつ RSI売られすぎ
        buy_cond = (df["close"] <= df["bb_lower"]) & (
            df[rsi_col] < self.params["rsi_oversold"]
        )
        signals[buy_cond] = 1

        # 売り: 価格がBB上限以上 かつ RSI買われすぎ
        sell_cond = (df["close"] >= df["bb_upper"]) & (
            df[rsi_col] > self.params["rsi_overbought"]
        )
        signals[sell_cond] = -1

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

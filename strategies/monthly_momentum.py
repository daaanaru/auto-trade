"""
月初モメンタム戦略

月の最初の3営業日に出来高の直近増加率が高い場合に買い、月末に手仕舞い。
出来高の急増はトレンド発生のシグナルになる、という仮説に基づく。

暗号資産では「月初の数日間」にモメンタムが発生しやすいという経験則がある。
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta


class MonthlyMomentumStrategy(BaseStrategy):
    """月初出来高モメンタム戦略"""

    def __init__(self, params=None):
        meta = StrategyMeta(
            name="Monthly_Momentum",
            market="crypto",
            version="1.0.0",
            description="Buy at month start when volume surges, sell at month end",
            tags=["momentum", "volume", "monthly"],
        )
        default_params = {
            "entry_days": 3,         # 月初の何営業日目までにエントリーするか
            "volume_ma_period": 20,  # 出来高移動平均の期間
            "volume_threshold": 1.5, # 出来高が平均の何倍以上でシグナル発生
            "risk_per_trade": 0.05,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=data.index)

        vol_ma = data["volume"].rolling(
            window=self.params["volume_ma_period"]
        ).mean()
        volume_ratio = data["volume"] / vol_ma

        for i in range(len(data)):
            dt = data.index[i]
            day_of_month = dt.day

            # 月初の指定日数以内 かつ 出来高が閾値以上 → 買い
            if (
                day_of_month <= self.params["entry_days"]
                and volume_ratio.iloc[i] >= self.params["volume_threshold"]
            ):
                signals.iloc[i] = 1

            # 月末の最終2営業日 → 手仕舞い（売り）
            # 翌営業日が翌月かどうかで判定
            if i < len(data) - 1:
                next_dt = data.index[i + 1]
                if next_dt.month != dt.month:
                    signals.iloc[i] = -1

        # 最終日は手仕舞い
        if len(signals) > 0:
            signals.iloc[-1] = -1

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

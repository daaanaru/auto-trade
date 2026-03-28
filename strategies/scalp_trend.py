"""
ScalpTrend戦略: マルチタイムフレーム・トレンドフォロー・スキャルピング

上様の実績手法を機械化:
  1. 上位足（1h/4h）でトレンド方向を判定
  2. 下位足（5m）でトレンド方向にエントリー
  3. RR比 2:1 で機械的にTP/SL

上位足トレンド判定:
  - EMA20 > EMA50 かつ close > EMA20 → 上昇トレンド（ロングのみ）
  - EMA20 < EMA50 かつ close < EMA20 → 下降トレンド（ショートのみ）
  - それ以外 → レンジ（エントリーなし）

下位足エントリー条件:
  - ロング: close が EMA8 を上抜け + EMA8 > EMA21 + 上位足が上昇トレンド
  - ショート: close が EMA8 を下抜け + EMA8 < EMA21 + 上位足が下降トレンド
"""

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta


class ScalpTrendStrategy(BaseStrategy):
    """マルチタイムフレーム・トレンドフォロー・スキャルピング"""

    def __init__(self, params: dict = None):
        meta = StrategyMeta(
            name="Scalp_Trend",
            market="crypto",
            version="1.0.0",
            description="Multi-timeframe trend-following scalp (upper: EMA20/50, lower: EMA8/21)",
            tags=["scalp", "trend", "multi-timeframe", "crypto"],
        )
        default_params = {
            # 上位足トレンド判定
            "upper_ema_fast": 20,
            "upper_ema_slow": 50,
            # 下位足エントリー
            "lower_ema_fast": 8,
            "lower_ema_slow": 21,
            # ポジションサイズ
            "risk_per_trade": 0.02,  # 1トレードあたりリスク2%
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)

    def detect_trend(self, upper_data: pd.DataFrame) -> str:
        """上位足（1h or 4h）からトレンド方向を判定する。

        Args:
            upper_data: 上位足のOHLCVデータ（columns: open, high, low, close, volume）

        Returns:
            "up" / "down" / "range"
        """
        if len(upper_data) < self.params["upper_ema_slow"] + 5:
            return "range"

        close = upper_data["close"]
        ema_fast = close.ewm(span=self.params["upper_ema_fast"], adjust=False).mean()
        ema_slow = close.ewm(span=self.params["upper_ema_slow"], adjust=False).mean()

        latest_close = float(close.iloc[-1])
        latest_fast = float(ema_fast.iloc[-1])
        latest_slow = float(ema_slow.iloc[-1])

        # EMA間の乖離率が0.1%未満 → トレンドなし（レンジ）
        ema_gap_pct = abs(latest_fast - latest_slow) / latest_slow if latest_slow != 0 else 0
        if ema_gap_pct < 0.001:
            return "range"

        if latest_fast > latest_slow and latest_close > latest_fast:
            return "up"
        elif latest_fast < latest_slow and latest_close < latest_fast:
            return "down"
        else:
            return "range"

    def generate_signals(self, data: pd.DataFrame, trend: str = "range") -> pd.Series:
        """下位足（5m）のエントリーシグナルを生成する。

        Args:
            data: 下位足OHLCVデータ
            trend: 上位足で判定したトレンド方向 ("up" / "down" / "range")

        Returns:
            pd.Series: 1(ロング) / -1(ショート) / 0(様子見)
        """
        signals = pd.Series(0, index=data.index)

        if len(data) < self.params["lower_ema_slow"] + 5:
            return signals

        close = data["close"]
        ema_fast = close.ewm(span=self.params["lower_ema_fast"], adjust=False).mean()
        ema_slow = close.ewm(span=self.params["lower_ema_slow"], adjust=False).mean()

        # EMAクロス判定（直近のクロスを検出）
        fast_above_slow = ema_fast > ema_slow
        close_above_fast = close > ema_fast
        close_below_fast = close < ema_fast

        # ロング条件: close > EMA8 かつ EMA8 > EMA21
        long_cond = close_above_fast & fast_above_slow
        # ショート条件: close < EMA8 かつ EMA8 < EMA21
        short_cond = close_below_fast & (~fast_above_slow)

        # 前のバーで条件が成立していなかった → 今のバーで成立 = 新規エントリーシグナル
        long_entry = long_cond & (~long_cond.shift(1, fill_value=False))
        short_entry = short_cond & (~short_cond.shift(1, fill_value=False))

        # トレンド方向に合致するシグナルだけを出す
        if trend == "up":
            signals[long_entry] = 1
        elif trend == "down":
            signals[short_entry] = -1
        # range の場合はシグナルなし（レンジ回避）

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.02)
        return (portfolio_value * risk_pct) / price

"""
FeatureEngine: テクニカル指標を一括計算する特徴量エンジン

taライブラリを使い、OHLCV DataFrameに各種指標カラムを追加する。
戦略クラスから呼び出して使う。

使い方:
    from plugins.indicators.feature_engine import FeatureEngine

    fe = FeatureEngine()
    df = fe.add_all(data)
    # df に rsi_14, macd, bb_upper 等のカラムが追加される
"""

import pandas as pd
import ta


class FeatureEngine:
    """テクニカル指標の一括計算エンジン。"""

    def add_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """全指標を追加して返す。元のDataFrameは変更しない。"""
        df = df.copy()
        df = self.add_rsi(df)
        df = self.add_macd(df)
        df = self.add_bollinger_bands(df)
        df = self.add_atr(df)
        df = self.add_obv(df)
        df = self.add_adx(df)
        return df

    def add_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """RSI（相対力指数）を追加する。

        Args:
            period: 計算期間（デフォルト14）
        """
        df[f"rsi_{period}"] = ta.momentum.RSIIndicator(
            close=df["close"], window=period
        ).rsi()
        return df

    def add_macd(
        self,
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> pd.DataFrame:
        """MACD（移動平均収束拡散法）を追加する。

        追加カラム: macd, macd_signal, macd_hist
        """
        macd_indicator = ta.trend.MACD(
            close=df["close"],
            window_fast=fast,
            window_slow=slow,
            window_sign=signal,
        )
        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_hist"] = macd_indicator.macd_diff()
        return df

    def add_bollinger_bands(
        self, df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
    ) -> pd.DataFrame:
        """ボリンジャーバンドを追加する。

        追加カラム: bb_upper, bb_middle, bb_lower, bb_pband, bb_wband
        """
        bb = ta.volatility.BollingerBands(
            close=df["close"], window=period, window_dev=std_dev
        )
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_pband"] = bb.bollinger_pband()  # %B（価格の位置）
        df["bb_wband"] = bb.bollinger_wband()  # バンド幅
        return df

    def add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """ATR（平均真のレンジ）を追加する。ボラティリティの指標。"""
        df[f"atr_{period}"] = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=period
        ).average_true_range()
        return df

    def add_obv(self, df: pd.DataFrame) -> pd.DataFrame:
        """OBV（出来高バランス）を追加する。出来高トレンドの指標。"""
        df["obv"] = ta.volume.OnBalanceVolumeIndicator(
            close=df["close"], volume=df["volume"]
        ).on_balance_volume()
        return df

    def add_mfi(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """MFI（マネーフローインデックス）を追加する。出来高加重のRSI。"""
        df[f"mfi_{period}"] = ta.volume.MFIIndicator(
            high=df["high"], low=df["low"], close=df["close"],
            volume=df["volume"], window=period,
        ).money_flow_index()
        return df

    def add_ema(self, df: pd.DataFrame, period: int = 200) -> pd.DataFrame:
        """EMA（指数移動平均）を追加する。"""
        df[f"ema_{period}"] = ta.trend.EMAIndicator(
            close=df["close"], window=period,
        ).ema_indicator()
        return df

    def add_volume_oscillator(
        self, df: pd.DataFrame, short: int = 5, long: int = 10
    ) -> pd.DataFrame:
        """ボリュームオシレーター（出来高の短期MA - 長期MAの差）を追加する。"""
        vol_short = df["volume"].rolling(window=short).mean()
        vol_long = df["volume"].rolling(window=long).mean()
        df["vol_osc"] = vol_short - vol_long
        return df

    def add_vwap(self, df: pd.DataFrame) -> pd.DataFrame:
        """VWAP（出来高加重平均価格）を追加する。

        日中足の場合はセッション単位でリセットするのが本来だが、
        バックテスト用にローリング累積VWAPとして計算する。
        """
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
        return df

    def add_adx(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """ADX（平均方向性指標）を追加する。トレンドの強さを計測。

        追加カラム: adx, adx_pos, adx_neg
        """
        adx_indicator = ta.trend.ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"], window=period
        )
        df["adx"] = adx_indicator.adx()
        df["adx_pos"] = adx_indicator.adx_pos()  # +DI
        df["adx_neg"] = adx_indicator.adx_neg()  # -DI
        return df

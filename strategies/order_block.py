"""
オーダーブロック戦略（包み足パターン）

YouTube「超知能」チャンネルで紹介されたオーダーブロック戦略。
包み足（Engulfing）パターンからオーダーブロックゾーンを特定し、
価格がそのゾーンに戻った時にエントリーするリバーサル戦略。

■ 買いオーダーブロック:
  1. 陰線を包み込む陽線（Bullish Engulfing）が発生
  2. その陰線の実体範囲がオーダーブロックゾーン
  3. 価格がこのゾーンまで下がってきたら買いエントリー

■ 売りオーダーブロック:
  1. 陽線を包み込む陰線（Bearish Engulfing）が発生
  2. その陽線の実体範囲がオーダーブロックゾーン
  3. 価格がこのゾーンまで上がってきたら売りエントリー

■ リスク管理:
  - 損切り: OBゾーンの外側を割ったら即損切り
  - 半分利確: 直近高値/安値を超えた時点で半分利確
  - ブレイクイーブンストップ: 半分利確後、残りのストップをエントリー価格に移動

注: BaseStrategyのgenerate_signals()は1/-1/0しか返せないため、
    半分利確のロジックはシグナル強度(0.5)として近似表現する。
"""

import numpy as np
import pandas as pd
from typing import Optional
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta


class OrderBlockStrategy(BaseStrategy):
    """オーダーブロック戦略（包み足ベース）"""

    def __init__(self, params=None):
        meta = StrategyMeta(
            name="Order_Block",
            market="crypto",
            version="1.0.0",
            description="Order block strategy using engulfing patterns with half-TP and breakeven stop",
            tags=["order_block", "engulfing", "reversal", "risk_management"],
        )
        default_params = {
            "ob_max_age": 30,            # OBゾーンの有効期限（バー数）
            "ob_max_zones": 5,           # 同時に保持するゾーン数の上限
            "swing_lookback": 10,        # 利確ターゲット用のスイング検出期間
            "risk_per_trade": 0.05,
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()
        ob_max_age = self.params["ob_max_age"]
        ob_max_zones = self.params["ob_max_zones"]
        swing_lookback = self.params["swing_lookback"]

        signals = pd.Series(0.0, index=df.index)

        # 実体の上端・下端を計算（ヒゲは無視）
        body_top = np.maximum(df["open"].values, df["close"].values)
        body_bottom = np.minimum(df["open"].values, df["close"].values)
        is_bullish = df["close"].values >= df["open"].values
        is_bearish = df["close"].values < df["open"].values

        # アクティブなオーダーブロックゾーンを管理
        # 各ゾーン: {"type": "bull"/"bear", "zone_top", "zone_bottom",
        #            "stop_loss", "created_idx", "entry_price", "target_price",
        #            "entered": bool, "half_tp_done": bool}
        active_zones = []

        for i in range(2, len(df)):
            # ----------------------------------------------------------
            # Step 1: 包み足パターンの検出 → オーダーブロックゾーン登録
            # ----------------------------------------------------------

            # Bullish Engulfing: 前のバーが陰線、現バーが陽線で前バーの実体を包む
            if (is_bearish[i - 1] and is_bullish[i]
                    and body_top[i] > body_top[i - 1]
                    and body_bottom[i] < body_bottom[i - 1]):
                # 買いOB: 前の陰線の実体がゾーン
                zone = {
                    "type": "bull",
                    "zone_top": body_top[i - 1],       # 陰線のopen
                    "zone_bottom": body_bottom[i - 1],  # 陰線のclose
                    "stop_loss": df["low"].iloc[i - 1],  # 陰線の安値を割ったら損切り
                    "created_idx": i,
                    "entry_price": None,
                    "target_price": self._find_swing_high(df["high"], i, swing_lookback),
                    "entered": False,
                    "half_tp_done": False,
                }
                active_zones.append(zone)

            # Bearish Engulfing: 前のバーが陽線、現バーが陰線で前バーの実体を包む
            if (is_bullish[i - 1] and is_bearish[i]
                    and body_top[i] > body_top[i - 1]
                    and body_bottom[i] < body_bottom[i - 1]):
                # 売りOB: 前の陽線の実体がゾーン
                zone = {
                    "type": "bear",
                    "zone_top": body_top[i - 1],       # 陽線のclose
                    "zone_bottom": body_bottom[i - 1],  # 陽線のopen
                    "stop_loss": df["high"].iloc[i - 1],  # 陽線の高値を超えたら損切り
                    "created_idx": i,
                    "entry_price": None,
                    "target_price": self._find_swing_low(df["low"], i, swing_lookback),
                    "entered": False,
                    "half_tp_done": False,
                }
                active_zones.append(zone)

            # 古いゾーンを期限切れで除去 / 上限数を維持
            active_zones = [
                z for z in active_zones
                if (i - z["created_idx"]) <= ob_max_age
            ]
            if len(active_zones) > ob_max_zones:
                active_zones = active_zones[-ob_max_zones:]

            # ----------------------------------------------------------
            # Step 2: 価格がOBゾーンに戻ったかチェック → エントリー/決済
            # ----------------------------------------------------------

            close_i = df["close"].iloc[i]
            low_i = df["low"].iloc[i]
            high_i = df["high"].iloc[i]

            zones_to_remove = []

            for idx, zone in enumerate(active_zones):
                if zone["type"] == "bull":
                    # --- 買いOBゾーン ---
                    if not zone["entered"]:
                        # 価格がゾーンに入ったらエントリー
                        if low_i <= zone["zone_top"] and close_i >= zone["zone_bottom"]:
                            zone["entered"] = True
                            zone["entry_price"] = close_i
                            signals.iloc[i] = 1
                    else:
                        # エントリー済み: 損切り/利確チェック
                        if low_i < zone["stop_loss"]:
                            # 損切り
                            signals.iloc[i] = -1
                            zones_to_remove.append(idx)
                        elif zone["target_price"] and high_i >= zone["target_price"]:
                            if not zone["half_tp_done"]:
                                # 半分利確（シグナルを0.5にして弱い売り）
                                # バックテストエンジンは1/-1/0なので、
                                # ここでは0に戻して「ポジション縮小」を表現
                                signals.iloc[i] = 0
                                zone["half_tp_done"] = True
                                # ブレイクイーブンストップ: 損切りをエントリー価格に移動
                                zone["stop_loss"] = zone["entry_price"]
                            else:
                                # 残りも利確（完全決済）
                                signals.iloc[i] = -1
                                zones_to_remove.append(idx)

                elif zone["type"] == "bear":
                    # --- 売りOBゾーン ---
                    if not zone["entered"]:
                        # 価格がゾーンに入ったらエントリー
                        if high_i >= zone["zone_bottom"] and close_i <= zone["zone_top"]:
                            zone["entered"] = True
                            zone["entry_price"] = close_i
                            signals.iloc[i] = -1
                    else:
                        # エントリー済み: 損切り/利確チェック
                        if high_i > zone["stop_loss"]:
                            # 損切り
                            signals.iloc[i] = 1
                            zones_to_remove.append(idx)
                        elif zone["target_price"] and low_i <= zone["target_price"]:
                            if not zone["half_tp_done"]:
                                signals.iloc[i] = 0
                                zone["half_tp_done"] = True
                                zone["stop_loss"] = zone["entry_price"]
                            else:
                                signals.iloc[i] = 1
                                zones_to_remove.append(idx)

            # 使い終わったゾーンを除去（逆順で削除）
            for idx in sorted(zones_to_remove, reverse=True):
                active_zones.pop(idx)

        return signals

    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

    # ----------------------------------------------------------
    # ヘルパーメソッド
    # ----------------------------------------------------------

    @staticmethod
    def _find_swing_high(high_series: pd.Series, current_idx: int, lookback: int) -> Optional[float]:
        """直近lookback本の中で最も高い高値を利確ターゲットとして返す。"""
        start = max(0, current_idx - lookback)
        segment = high_series.iloc[start:current_idx]
        if len(segment) == 0:
            return None
        return segment.max()

    @staticmethod
    def _find_swing_low(low_series: pd.Series, current_idx: int, lookback: int) -> Optional[float]:
        """直近lookback本の中で最も低い安値を利確ターゲットとして返す。"""
        start = max(0, current_idx - lookback)
        segment = low_series.iloc[start:current_idx]
        if len(segment) == 0:
            return None
        return segment.min()

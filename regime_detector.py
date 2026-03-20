#!/usr/bin/env python3
"""
レジーム判定モジュール — 市場環境を自動分類し戦略選択を切り替える

3つのレジームを判定する:
  TREND  — 明確なトレンドが発生中。トレンドフォロー戦略を優先
  RANGE  — レンジ相場。平均回帰戦略を優先
  CRISIS — 高ボラティリティの危機的環境。新規エントリー抑制

判定指標:
  - VIX: 恐怖指数。25超でCRISIS候補
  - DXY(ドル指数) SMA20乖離: トレンドの方向性
  - ADX(市場固有): 個別銘柄/市場のトレンド強度

設計方針（はる方式 @Safty_First_）:
  トレンド/レンジの判断だけルールベースで行い、
  トレンド用(SMA Crossover, Monthly Momentum)とレンジ用(BB+RSI)を自動切替する。

使い方:
  from regime_detector import RegimeDetector
  detector = RegimeDetector()
  regime = detector.detect()  # "TREND" | "RANGE" | "CRISIS"

  # 戦略選択
  strategies = detector.recommended_strategies(regime)

  # 個別銘柄のADX判定
  regime_local = detector.detect_local(data)  # DataFrameからADXベースで判定
"""

import warnings
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf


@dataclass
class RegimeResult:
    """レジーム判定結果"""
    regime: str           # "TREND" | "RANGE" | "CRISIS"
    confidence: float     # 0.0〜1.0
    vix: Optional[float] = None
    dxy: Optional[float] = None
    dxy_sma20: Optional[float] = None
    dxy_deviation: Optional[float] = None  # DXY SMA20乖離率(%)
    adx: Optional[float] = None            # ローカルADX（個別銘柄用）
    reason: str = ""

    def __str__(self):
        return f"[{self.regime}] conf={self.confidence:.0%} | {self.reason}"


# レジーム閾値
VIX_CRISIS_THRESHOLD = 28.0     # VIX 28超 → CRISIS
VIX_ELEVATED_THRESHOLD = 22.0   # VIX 22超 → CRISIS候補（他条件と複合）
DXY_TREND_DEVIATION = 1.5       # DXY SMA20乖離 ±1.5%超 → 通貨トレンド
ADX_TREND_THRESHOLD = 25.0      # ADX 25超 → 個別銘柄トレンド
ADX_STRONG_TREND = 35.0         # ADX 35超 → 強いトレンド


class RegimeDetector:
    """市場レジーム判定器"""

    def __init__(self):
        self._cache = {}
        self._cache_time = None
        self._cache_ttl = 3600  # 1時間

    def _fetch_macro(self) -> dict:
        """VIX/DXYをyfinanceから取得。1時間キャッシュ。"""
        now = time.time()
        if self._cache_time and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        data = {}
        targets = [
            ("^VIX", "1mo"),
            ("DX-Y.NYB", "3mo"),
        ]

        for ticker, period in targets:
            for attempt in range(3):
                try:
                    df = yf.download(ticker, period=period, interval="1d", progress=False)
                    if df.empty:
                        break
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.droplevel(1)

                    if ticker == "^VIX":
                        data["vix"] = float(df["Close"].iloc[-1])
                    elif ticker == "DX-Y.NYB":
                        data["dxy"] = float(df["Close"].iloc[-1])
                        sma20 = df["Close"].rolling(20).mean()
                        if not sma20.isna().iloc[-1]:
                            data["dxy_sma20"] = float(sma20.iloc[-1])
                        else:
                            data["dxy_sma20"] = data["dxy"]
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2 ** attempt)

        self._cache = data
        self._cache_time = now
        return data

    def detect(self) -> RegimeResult:
        """
        グローバルレジームを判定する。
        VIX + DXY乖離の組み合わせでTREND/RANGE/CRISISを分類。
        """
        macro = self._fetch_macro()

        vix = macro.get("vix")
        dxy = macro.get("dxy")
        dxy_sma20 = macro.get("dxy_sma20")

        # データ取得失敗時はRANGEをデフォルトとする（安全側）
        if vix is None and dxy is None:
            return RegimeResult(
                regime="RANGE",
                confidence=0.3,
                reason="マクロデータ取得失敗。デフォルトRANGE",
            )

        dxy_deviation = 0.0
        if dxy and dxy_sma20 and dxy_sma20 > 0:
            dxy_deviation = (dxy - dxy_sma20) / dxy_sma20 * 100

        reasons = []
        scores = {"TREND": 0.0, "RANGE": 0.0, "CRISIS": 0.0}

        # --- VIX判定 ---
        if vix is not None:
            if vix >= VIX_CRISIS_THRESHOLD:
                scores["CRISIS"] += 0.6
                reasons.append(f"VIX={vix:.1f}(危機水準{VIX_CRISIS_THRESHOLD}超)")
            elif vix >= VIX_ELEVATED_THRESHOLD:
                scores["CRISIS"] += 0.3
                scores["TREND"] += 0.1
                reasons.append(f"VIX={vix:.1f}(警戒水準)")
            else:
                scores["RANGE"] += 0.2
                reasons.append(f"VIX={vix:.1f}(平穏)")

        # --- DXY乖離判定 ---
        if abs(dxy_deviation) >= DXY_TREND_DEVIATION:
            scores["TREND"] += 0.4
            direction = "ドル高" if dxy_deviation > 0 else "ドル安"
            reasons.append(f"DXY乖離{dxy_deviation:+.1f}%({direction}トレンド)")
        else:
            scores["RANGE"] += 0.3
            reasons.append(f"DXY乖離{dxy_deviation:+.1f}%(レンジ)")

        # 判定
        regime = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[regime] / total if total > 0 else 0.5

        return RegimeResult(
            regime=regime,
            confidence=confidence,
            vix=vix,
            dxy=dxy,
            dxy_sma20=dxy_sma20,
            dxy_deviation=dxy_deviation,
            reason=" / ".join(reasons),
        )

    def detect_local(self, data: pd.DataFrame) -> RegimeResult:
        """
        個別銘柄のDataFrameからADXベースでローカルレジームを判定。
        グローバルレジームと組み合わせて使う。

        Args:
            data: OHLCVデータ（最低14日分必要）
        """
        if len(data) < 14:
            return RegimeResult(
                regime="RANGE",
                confidence=0.3,
                reason="データ不足(14日未満)。デフォルトRANGE",
            )

        try:
            import ta
            adx_indicator = ta.trend.ADXIndicator(
                high=data["high"], low=data["low"], close=data["close"], window=14
            )
            adx_val = float(adx_indicator.adx().iloc[-1])
        except Exception:
            return RegimeResult(
                regime="RANGE",
                confidence=0.3,
                reason="ADX計算失敗。デフォルトRANGE",
            )

        if adx_val >= ADX_STRONG_TREND:
            return RegimeResult(
                regime="TREND",
                confidence=0.85,
                adx=adx_val,
                reason=f"ADX={adx_val:.1f}(強トレンド{ADX_STRONG_TREND}超)",
            )
        elif adx_val >= ADX_TREND_THRESHOLD:
            return RegimeResult(
                regime="TREND",
                confidence=0.65,
                adx=adx_val,
                reason=f"ADX={adx_val:.1f}(トレンド{ADX_TREND_THRESHOLD}超)",
            )
        else:
            return RegimeResult(
                regime="RANGE",
                confidence=0.7,
                adx=adx_val,
                reason=f"ADX={adx_val:.1f}(レンジ{ADX_TREND_THRESHOLD}以下)",
            )

    def recommended_strategies(self, regime: str, market: str = "us") -> list[str]:
        """
        レジームに応じた推奨戦略リストを返す。

        Returns:
            戦略名のリスト（param_keyに対応）。優先度順。
        """
        if regime == "CRISIS":
            # 危機時: VolScale（ボラ適応型）のみ。bb_rsi/monthlyは新規エントリー抑制
            return ["volscale_sma"]

        if regime == "TREND":
            # トレンド時: トレンドフォロー系を優先
            return ["monthly", "sma_crossover", "volscale_sma"]

        # RANGE: 平均回帰系を優先
        return ["bb_rsi", "volscale_sma"]

    def should_suppress_entry(self, regime: str) -> bool:
        """CRISISレジームでの新規エントリー抑制判定。"""
        return regime == "CRISIS"


# === CLI: 単体実行で現在のレジームを表示 ===
if __name__ == "__main__":
    detector = RegimeDetector()

    print("=== グローバルレジーム判定 ===")
    result = detector.detect()
    print(f"  レジーム: {result.regime}")
    print(f"  信頼度: {result.confidence:.0%}")
    print(f"  VIX: {result.vix}")
    print(f"  DXY: {result.dxy} (SMA20: {result.dxy_sma20})")
    print(f"  DXY乖離: {result.dxy_deviation:+.1f}%")
    print(f"  理由: {result.reason}")
    print()
    print(f"  推奨戦略: {detector.recommended_strategies(result.regime)}")
    print(f"  エントリー抑制: {detector.should_suppress_entry(result.regime)}")

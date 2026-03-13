#!/usr/bin/env python3
"""
市場別ファンダメンタル分析モジュール

市場ごとに異なるスコアリングロジックを提供する。
- 株式（日本株・米国株）: PER/PBR/配当/売上成長/時価総額/52週高値
- FX: DXY/VIX/米10年金利から通貨ペア別にバイアス判定
- BTC/暗号資産: 半減期サイクル/DXY/VIX
- ゴールド: VIX/実質金利代理/DXY

全て -1.0〜1.0 のスコアと理由文字列を返す。
"""

import time
from datetime import datetime

import pandas as pd
import yfinance as yf

# ==============================================================
# マクロデータキャッシュ（1時間有効）
# ==============================================================

_macro_cache = {}
_macro_cache_time = None
CACHE_TTL = 3600  # 1時間


def _get_macro_data() -> dict:
    """DXY、VIX、米10年金利をまとめて取得。1時間キャッシュ。最大3回リトライ。"""
    global _macro_cache, _macro_cache_time
    now = time.time()
    if _macro_cache_time and (now - _macro_cache_time) < CACHE_TTL:
        return _macro_cache

    data = {}

    # 取得対象: (キー名, ティッカー, 期間, 取得する値のリスト)
    targets = [
        {
            "ticker": "DX=F",
            "period": "3mo",
            "extract": lambda df: _extract_dxy(df),
        },
        {
            "ticker": "^VIX",
            "period": "1mo",
            "extract": lambda df: _extract_vix(df),
        },
        {
            "ticker": "^TNX",
            "period": "3mo",
            "extract": lambda df: _extract_us10y(df),
        },
    ]

    for target in targets:
        for attempt in range(3):
            try:
                df = yf.download(
                    target["ticker"],
                    period=target["period"],
                    interval="1d",
                    progress=False,
                )
                if df.empty:
                    break
                # MultiIndex対策
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                extracted = target["extract"](df)
                data.update(extracted)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 指数バックオフ: 1秒, 2秒

    # USDJPYも取得（JPYクロス判定用）
    for attempt in range(3):
        try:
            df = yf.download("USDJPY=X", period="1mo", interval="1d", progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                data["usdjpy"] = float(df["Close"].iloc[-1])
                if len(df) > 5:
                    data["usdjpy_prev"] = float(df["Close"].iloc[-5])
                else:
                    data["usdjpy_prev"] = data["usdjpy"]
            break
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)

    _macro_cache = data
    _macro_cache_time = now
    return data


def _extract_dxy(df: pd.DataFrame) -> dict:
    """DXYデータからトレンド情報を抽出する。"""
    result = {}
    result["dxy"] = float(df["Close"].iloc[-1])
    result["dxy_sma20"] = float(df["Close"].rolling(20).mean().iloc[-1])
    if len(df) > 22:
        result["dxy_change_1m"] = float(
            (df["Close"].iloc[-1] / df["Close"].iloc[-22] - 1) * 100
        )
    else:
        result["dxy_change_1m"] = 0.0
    return result


def _extract_vix(df: pd.DataFrame) -> dict:
    """VIXデータを抽出する。"""
    return {"vix": float(df["Close"].iloc[-1])}


def _extract_us10y(df: pd.DataFrame) -> dict:
    """米10年国債利回りデータを抽出する。"""
    result = {}
    result["us10y"] = float(df["Close"].iloc[-1])
    if len(df) > 22:
        result["us10y_prev"] = float(df["Close"].iloc[-22])
    else:
        result["us10y_prev"] = result["us10y"]
    return result


# ==============================================================
# 株式用（既存ロジック移植）
# ==============================================================


def get_stock_score(symbol: str) -> dict:
    """yfinanceからファンダメンタル情報を取得してスコア化する（株式用）。

    PER/PBR/配当/売上成長/時価総額/52週高値を見る。

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        score = 0.0
        reasons = []

        # PER（株価収益率）
        pe = info.get("trailingPE") or info.get("forwardPE")
        if pe:
            if pe < 15:
                score += 0.3
                reasons.append(f"PER低い({pe:.1f})")
            elif pe > 40:
                score -= 0.2
                reasons.append(f"PER高い({pe:.1f})")

        # PBR（株価純資産倍率）
        pb = info.get("priceToBook")
        if pb:
            if pb < 1.5:
                score += 0.2
                reasons.append(f"PBR割安({pb:.1f})")
            elif pb > 5:
                score -= 0.1
                reasons.append(f"PBR割高({pb:.1f})")

        # 配当利回り
        div_yield = info.get("dividendYield")
        if div_yield and div_yield > 3.0:
            score += 0.2
            reasons.append(f"高配当({div_yield:.2f}%)")

        # 売上成長率
        rev_growth = info.get("revenueGrowth")
        if rev_growth:
            if rev_growth > 0.1:
                score += 0.3
                reasons.append(f"売上成長({rev_growth*100:.0f}%)")
            elif rev_growth < -0.1:
                score -= 0.3
                reasons.append(f"売上減少({rev_growth*100:.0f}%)")

        # 時価総額（小型株は避ける）
        market_cap = info.get("marketCap")
        if market_cap and market_cap < 1e9:
            score -= 0.1
            reasons.append("小型株")

        # 52週高値からの距離
        fifty_two_high = info.get("fiftyTwoWeekHigh")
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if fifty_two_high and current_price:
            ratio = current_price / fifty_two_high
            if ratio > 0.95:
                score -= 0.1
                reasons.append("52週高値圏")
            elif ratio < 0.7:
                score += 0.1
                reasons.append("52週安値圏")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 2),
            "reason": " / ".join(reasons) if reasons else "情報不足",
            "data": {
                "pe": pe,
                "pb": pb,
                "div_yield": f"{div_yield:.1f}%" if div_yield else None,
                "rev_growth": f"{rev_growth*100:.0f}%" if rev_growth else None,
                "market_cap": market_cap,
            },
        }
    except Exception as e:
        return {"score": -1.0, "reason": f"取得失敗: {e}", "data": {}}


# ==============================================================
# FX用
# ==============================================================

# USDが分子（USD/xxx）のペア: ドル高→BUY方向
_USD_BASE_PAIRS = {"USDJPY=X", "USDCHF=X", "USDCAD=X"}

# USDが分母（xxx/USD）のペア: ドル高→SELL方向
_USD_QUOTE_PAIRS = {"EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X"}

# JPYクロス（xxxJPY）: 円安トレンドならBUY方向
_JPY_CROSS_PAIRS = {"EURJPY=X", "GBPJPY=X", "AUDJPY=X"}


def get_fx_score(symbol: str) -> dict:
    """FX通貨ペアのファンダメンタルスコアを算出する。

    DXYトレンド、通貨ペアとDXYの関係、VIX、金利差で判定。

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
    """
    try:
        macro = _get_macro_data()
        score = 0.0
        reasons = []

        dxy = macro.get("dxy")
        dxy_sma20 = macro.get("dxy_sma20")
        vix = macro.get("vix")
        us10y = macro.get("us10y")
        us10y_prev = macro.get("us10y_prev")
        usdjpy = macro.get("usdjpy")
        usdjpy_prev = macro.get("usdjpy_prev")

        # 1. DXYトレンド判定
        dxy_bullish = None  # True=ドル高, False=ドル安
        if dxy and dxy_sma20:
            dxy_bullish = dxy > dxy_sma20

        # 2. 通貨ペアとDXYの関係
        if dxy_bullish is not None:
            sym_upper = symbol.upper()
            if sym_upper in _USD_BASE_PAIRS:
                # USD/xxx: ドル高→BUY
                adj = 0.3 if dxy_bullish else -0.3
                score += adj
                reasons.append(f"DXY{'強' if dxy_bullish else '弱'}({dxy:.1f})")
            elif sym_upper in _USD_QUOTE_PAIRS:
                # xxx/USD: ドル高→SELL
                adj = -0.3 if dxy_bullish else 0.3
                score += adj
                reasons.append(f"DXY{'強' if dxy_bullish else '弱'}({dxy:.1f})")
            elif sym_upper in _JPY_CROSS_PAIRS:
                # JPYクロス: 円安(USDJPY上昇)→BUY
                if usdjpy and usdjpy_prev:
                    if usdjpy > usdjpy_prev:
                        score += 0.2
                        reasons.append(f"円安傾向(USDJPY={usdjpy:.1f})")
                    else:
                        score -= 0.2
                        reasons.append(f"円高傾向(USDJPY={usdjpy:.1f})")
            else:
                # 不明な通貨ペアは中立
                pass

        # 3. VIXによるリスク判定
        if vix:
            sym_upper = symbol.upper()
            if vix > 25:
                # リスクオフ: 円高・ドル高バイアス
                if sym_upper in _JPY_CROSS_PAIRS or sym_upper == "USDJPY=X":
                    score -= 0.2
                    reasons.append(f"VIX高(リスクオフ)={vix:.1f}")
                elif sym_upper in _USD_BASE_PAIRS:
                    score += 0.1
                    reasons.append(f"VIX高→ドル買い={vix:.1f}")
            elif vix < 15:
                # リスクオン: 高金利通貨に有利
                score += 0.1
                reasons.append(f"VIX低(リスクオン)={vix:.1f}")

        # 4. 金利差（米10年金利の方向）
        if us10y and us10y_prev:
            sym_upper = symbol.upper()
            rate_rising = us10y > us10y_prev
            if sym_upper in _USD_BASE_PAIRS:
                adj = 0.2 if rate_rising else -0.2
                score += adj
                reasons.append(f"米金利{'上昇' if rate_rising else '低下'}({us10y:.2f}%)")
            elif sym_upper in _USD_QUOTE_PAIRS:
                adj = -0.2 if rate_rising else 0.2
                score += adj
                reasons.append(f"米金利{'上昇' if rate_rising else '低下'}({us10y:.2f}%)")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 2),
            "reason": " / ".join(reasons) if reasons else "マクロデータ不足",
            "data": {
                "dxy": dxy,
                "dxy_sma20": dxy_sma20,
                "vix": vix,
                "us10y": us10y,
            },
        }
    except Exception as e:
        return {"score": 0.0, "reason": f"FXスコア取得失敗: {e}", "data": {}}


# ==============================================================
# BTC/暗号資産用
# ==============================================================

# 直近の半減期日（2024-04-19）
_HALVING_DATE = datetime(2024, 4, 19)


def get_btc_score(symbol: str) -> dict:
    """BTC/暗号資産のファンダメンタルスコアを算出する。

    半減期サイクル位置、DXY逆相関、VIXで判定。
    ETH/XRP/XLM/MONAもBTCと同じロジック（BTCに連動するため）。

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
    """
    try:
        macro = _get_macro_data()
        score = 0.0
        reasons = []

        # 1. 半減期サイクル位置（最重要）
        now = datetime.now()
        months_since_halving = (now - _HALVING_DATE).days / 30.44  # 平均月数

        if months_since_halving < 6:
            cycle_score = 0.1
            cycle_phase = "蓄積期"
        elif months_since_halving < 12:
            cycle_score = 0.3
            cycle_phase = "上昇初期"
        elif months_since_halving < 18:
            cycle_score = 0.4
            cycle_phase = "上昇中期"
        elif months_since_halving < 24:
            cycle_score = 0.2
            cycle_phase = "ピーク圏"
        elif months_since_halving < 36:
            cycle_score = -0.2
            cycle_phase = "下落リスク"
        else:
            cycle_score = 0.1
            cycle_phase = "底値圏"

        score += cycle_score
        reasons.append(
            f"半減期{months_since_halving:.0f}ヶ月後({cycle_phase}:{cycle_score:+.1f})"
        )

        # 2. DXYとの逆相関
        dxy = macro.get("dxy")
        dxy_sma20 = macro.get("dxy_sma20")
        if dxy and dxy_sma20:
            if dxy < dxy_sma20:
                score += 0.2
                reasons.append(f"DXY弱({dxy:.1f}<SMA{dxy_sma20:.1f})")
            else:
                score -= 0.2
                reasons.append(f"DXY強({dxy:.1f}>SMA{dxy_sma20:.1f})")

        # 3. VIX
        vix = macro.get("vix")
        if vix:
            if vix > 30:
                score -= 0.2
                reasons.append(f"VIX極高({vix:.1f})リスクオフ")
            elif vix < 20:
                score += 0.1
                reasons.append(f"VIX低({vix:.1f})リスクオン")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 2),
            "reason": " / ".join(reasons) if reasons else "データ不足",
            "data": {
                "months_since_halving": round(months_since_halving, 1),
                "cycle_phase": cycle_phase,
                "dxy": dxy,
                "vix": vix,
            },
        }
    except Exception as e:
        return {"score": 0.0, "reason": f"BTCスコア取得失敗: {e}", "data": {}}


# ==============================================================
# ゴールド用
# ==============================================================


def get_gold_score() -> dict:
    """ゴールドのファンダメンタルスコアを算出する。

    VIX（リスクオフで金上昇）、実質金利代理（名目金利方向）、DXY（ドル安で金上昇）で判定。

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
    """
    try:
        macro = _get_macro_data()
        score = 0.0
        reasons = []

        # 1. VIX（恐怖指数）
        vix = macro.get("vix")
        if vix:
            if vix > 25:
                score += 0.3
                reasons.append(f"VIX高({vix:.1f})リスクオフ→金買い")
            elif vix > 20:
                score += 0.1
                reasons.append(f"VIXやや高({vix:.1f})")
            elif vix < 15:
                score -= 0.2
                reasons.append(f"VIX低({vix:.1f})リスクオン→金弱い")

        # 2. 実質金利の代理指標（名目金利の方向で代替）
        us10y = macro.get("us10y")
        us10y_prev = macro.get("us10y_prev")
        if us10y and us10y_prev:
            if us10y < us10y_prev:
                score += 0.3
                reasons.append(f"米金利低下({us10y:.2f}%←{us10y_prev:.2f}%)→金買い")
            else:
                score -= 0.2
                reasons.append(f"米金利上昇({us10y:.2f}%←{us10y_prev:.2f}%)→金売り")

        # 3. DXY（ドル弱い→金高い）
        dxy = macro.get("dxy")
        dxy_sma20 = macro.get("dxy_sma20")
        if dxy and dxy_sma20:
            if dxy < dxy_sma20:
                score += 0.2
                reasons.append(f"DXY弱({dxy:.1f}<SMA{dxy_sma20:.1f})→金買い")
            else:
                score -= 0.2
                reasons.append(f"DXY強({dxy:.1f}>SMA{dxy_sma20:.1f})→金売り")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 2),
            "reason": " / ".join(reasons) if reasons else "マクロデータ不足",
            "data": {
                "vix": vix,
                "us10y": us10y,
                "us10y_prev": us10y_prev,
                "dxy": dxy,
                "dxy_sma20": dxy_sma20,
            },
        }
    except Exception as e:
        return {"score": 0.0, "reason": f"ゴールドスコア取得失敗: {e}", "data": {}}


# ==============================================================
# 統合関数
# ==============================================================


def get_market_fundamental_score(symbol: str, market: str) -> dict:
    """市場に応じた適切なファンダメンタルスコアを返す。

    Args:
        symbol: ティッカーシンボル（例: "AAPL", "USDJPY=X", "BTC-USD", "GLD"）
        market: 市場識別子（"jp", "us", "fx", "btc", "gold"）

    Returns:
        {"score": float(-1~1), "reason": str, "data": dict}
    """
    if market in ("jp", "us"):
        return get_stock_score(symbol)
    elif market == "fx":
        return get_fx_score(symbol)
    elif market == "btc":
        return get_btc_score(symbol)
    elif market == "gold":
        return get_gold_score()
    else:
        return get_stock_score(symbol)


# ==============================================================
# テスト用
# ==============================================================

if __name__ == "__main__":
    print("=== 市場別ファンダメンタルスコア テスト ===\n")

    tests = [
        ("AAPL", "us", "米国株"),
        ("6758.T", "jp", "日本株"),
        ("USDJPY=X", "fx", "FX(USD/JPY)"),
        ("EURUSD=X", "fx", "FX(EUR/USD)"),
        ("BTC-USD", "btc", "BTC"),
        ("GLD", "gold", "ゴールド"),
    ]

    for symbol, market, label in tests:
        print(f"--- {label}: {symbol} ---")
        result = get_market_fundamental_score(symbol, market)
        print(f"  スコア: {result['score']:+.2f}")
        print(f"  理由: {result['reason']}")
        print(f"  データ: {result['data']}")
        print()

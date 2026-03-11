#!/usr/bin/env python3
"""
jp_fullmarket_scanner.py -- 東証全上場株スキャナー

東証全上場株（約3,800銘柄）から急上昇シグナルを検出する。
JPXの上場銘柄一覧を取得し、2段階スキャンで候補を絞り込む。

Phase 1（粗いフィルター）: 全銘柄をバッチダウンロードで高速スキャン
Phase 2（詳細分析）: 候補銘柄の個別情報を並列取得してスコアリング

使い方:
    python3 jp_fullmarket_scanner.py              # 通常実行
    python3 jp_fullmarket_scanner.py --save        # JSON保存
    python3 jp_fullmarket_scanner.py --top 30      # 上位30件表示（デフォルト20）
    python3 jp_fullmarket_scanner.py --refresh     # ティッカーリストを再取得
    python3 jp_fullmarket_scanner.py --min-score 5 # 最低スコアフィルター
"""

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 通知モジュール（単体実行時にも動くように）
try:
    from notifier import send_discord_embed
except ImportError:
    def send_discord_embed(*args, **kwargs):
        return False

# tqdmがあれば使う
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# richがあれば使う
try:
    from rich.console import Console
    from rich.table import Table
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ==============================================================
# 定数
# ==============================================================

JPX_XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "misc/tvdivq0000001vg2-att/data_j.xls"
)
TICKER_CACHE_FILE = os.path.join(BASE_DIR, "jpx_listed_stocks.csv")
RESULT_FILE = os.path.join(BASE_DIR, "fullmarket_scan_results.json")

# コンプライアンス除外リスト
EXCLUDED_TICKERS = {
    # 利害関係のある関連銘柄は除外
    "9984.T",   # ソフトバンクグループ
    "9434.T",   # ソフトバンク（通信）
    "4689.T",   # LINEヤフー
    "2148.T",   # アイティメディア
    "3092.T",   # ZOZO
    "2678.T",   # アスクル
    "2491.T",   # バリューコマース
    "2484.T",   # 出前館
    "4498.T",   # サイバートラスト
    "7036.T",   # イーエムネットジャパン
    "7115.T",   # SBGグループ企業
    "299A.T",   # SBGグループ企業
}

# バッチダウンロード設定
BATCH_SIZE = 100          # 1回のyf.download()で取得する銘柄数
PHASE2_WORKERS = 5        # Phase 2のinfo取得並列数
MAX_RETRIES = 3           # ネットワークエラー時のリトライ回数


# ==============================================================
# ティッカーリスト取得
# ==============================================================

def download_jpx_list() -> pd.DataFrame:
    """JPXの上場銘柄一覧xlsをダウンロードしてDataFrameで返す。

    xlsファイルを読み込み、普通株式のみにフィルターする。
    ダウンロード失敗時はリトライする。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(
                "JPX上場銘柄一覧をダウンロード中... (試行 %d/%d)",
                attempt, MAX_RETRIES,
            )
            df = pd.read_excel(JPX_XLS_URL)
            logger.info("ダウンロード完了: %d行", len(df))
            return df
        except Exception as e:
            logger.warning("ダウンロード失敗 (試行 %d): %s", attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(
        "JPXの銘柄一覧を取得できませんでした。"
        "ネットワーク接続を確認するか、--refresh なしで再実行してください。"
    )


def load_ticker_list(refresh: bool = False) -> list[dict]:
    """ティッカーリストを取得する。

    キャッシュがあればそれを使い、なければJPXからダウンロードする。
    --refresh指定時は強制再取得。

    Returns:
        [{"code": "7203.T", "name": "トヨタ自動車", "sector": "輸送用機器"}, ...]
    """
    if not refresh and os.path.exists(TICKER_CACHE_FILE):
        logger.info("キャッシュから銘柄リスト読み込み: %s", TICKER_CACHE_FILE)
        df = pd.read_csv(TICKER_CACHE_FILE, dtype={"code": str})
        tickers = df.to_dict("records")
        logger.info("読み込み完了: %d銘柄", len(tickers))
        return tickers

    # JPXからダウンロード
    raw_df = download_jpx_list()

    # カラム名を正規化（JPXのxlsはカラム名が日本語）
    # 主要カラム: コード, 銘柄名, 市場・商品区分, 33業種区分
    tickers = []
    for _, row in raw_df.iterrows():
        try:
            code_val = row.get("コード")
            if pd.isna(code_val):
                continue
            code = str(int(code_val))

            name = str(row.get("銘柄名", ""))
            market_segment = str(row.get("市場・商品区分", ""))
            sector = str(row.get("33業種区分", ""))

            # 普通株式のみ（ETF, REIT, インフラファンド, ETN等を除外）
            # 市場区分に「内国株式」が含まれるもの、または
            # プライム/スタンダード/グロースのいずれかに該当するもの
            is_stock = False
            for keyword in ["プライム", "スタンダード", "グロース"]:
                if keyword in market_segment:
                    is_stock = True
                    break

            if not is_stock:
                continue

            ticker = f"{code}.T"

            # SBGグループ除外
            if ticker in EXCLUDED_TICKERS:
                continue

            tickers.append({
                "code": ticker,
                "name": name,
                "sector": sector,
                "market": market_segment,
            })
        except (ValueError, TypeError):
            continue

    logger.info("普通株式: %d銘柄（除外後）", len(tickers))

    # キャッシュに保存
    cache_df = pd.DataFrame(tickers)
    cache_df.to_csv(TICKER_CACHE_FILE, index=False)
    logger.info("キャッシュ保存: %s", TICKER_CACHE_FILE)

    return tickers


# ==============================================================
# テクニカル指標計算
# ==============================================================

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI（相対力指数）を計算する。"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_ema(series: pd.Series, span: int) -> pd.Series:
    """EMA（指数移動平均）を計算する。"""
    return series.ewm(span=span, adjust=False).mean()


def calc_macd(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """MACDとシグナルラインを計算する。

    Returns:
        (macd_line, signal_line)
    """
    ema12 = calc_ema(close, 12)
    ema26 = calc_ema(close, 26)
    macd_line = ema12 - ema26
    signal_line = calc_ema(macd_line, 9)
    return macd_line, signal_line


def calc_bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    """ボリンジャーバンドを計算する。

    Returns:
        (upper, middle, lower, pct_b)
        pct_bは0-1の範囲で現在値のバンド内位置を示す。
    """
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    bandwidth = upper - lower
    pct_b = (close - lower) / bandwidth.replace(0, np.nan)
    return upper, middle, lower, pct_b


# ==============================================================
# Phase 1: 粗いフィルター
# ==============================================================

def detect_signals_batch(
    ohlcv: pd.DataFrame,
    ticker: str,
) -> Optional[dict]:
    """1銘柄のOHLCVデータからシグナルを検出する。

    Args:
        ohlcv: 日足データ（Open, High, Low, Close, Volume列）
        ticker: 銘柄コード（例: "7203.T"）

    Returns:
        シグナルが1つ以上あれば辞書を返す。なければNone。
    """
    if ohlcv is None or len(ohlcv) < 25:
        return None

    try:
        close = ohlcv["Close"] if "Close" in ohlcv.columns else ohlcv["close"]
        open_ = ohlcv["Open"] if "Open" in ohlcv.columns else ohlcv["open"]
        high = ohlcv["High"] if "High" in ohlcv.columns else ohlcv["high"]
        volume = ohlcv["Volume"] if "Volume" in ohlcv.columns else ohlcv["volume"]
    except KeyError:
        return None

    if len(close.dropna()) < 25:
        return None

    signals = []
    score = 0

    # 1. 出来高急騰: 直近出来高が20日移動平均の2倍以上
    vol_ma20 = volume.rolling(window=20).mean()
    if not pd.isna(vol_ma20.iloc[-1]) and vol_ma20.iloc[-1] > 0:
        vol_ratio = float(volume.iloc[-1] / vol_ma20.iloc[-1])
        if vol_ratio >= 2.0:
            signals.append("出来高急騰")
            score += 3
    else:
        vol_ratio = 0.0

    # 2. ブレイクアウト: 終値が直近20日高値を更新
    high_20d = high.rolling(window=20).max()
    if not pd.isna(high_20d.iloc[-2]):
        if float(close.iloc[-1]) > float(high_20d.iloc[-2]):
            signals.append("ブレイクアウト")
            score += 3

    # 3. ギャップアップ: 当日始値が前日終値比+2%以上
    if len(close) >= 2:
        prev_close = float(close.iloc[-2])
        today_open = float(open_.iloc[-1])
        if prev_close > 0:
            gap_pct = (today_open - prev_close) / prev_close * 100
            if gap_pct >= 2.0:
                signals.append("ギャップアップ")
                score += 2

    # 4. 連続陽線: 3日連続で終値 > 始値
    if len(close) >= 3:
        bullish_days = all(
            float(close.iloc[-i]) > float(open_.iloc[-i])
            for i in range(1, 4)
        )
        if bullish_days:
            signals.append("連続陽線")
            score += 1

    # 5. ゴールデンクロス: 5日EMAが25日EMAを上抜け
    ema5 = calc_ema(close, 5)
    ema25 = calc_ema(close, 25)
    if len(ema5) >= 2 and len(ema25) >= 2:
        if (not pd.isna(ema5.iloc[-1]) and not pd.isna(ema25.iloc[-1])
                and not pd.isna(ema5.iloc[-2]) and not pd.isna(ema25.iloc[-2])):
            today_cross = float(ema5.iloc[-1]) > float(ema25.iloc[-1])
            yesterday_below = float(ema5.iloc[-2]) <= float(ema25.iloc[-2])
            if today_cross and yesterday_below:
                signals.append("ゴールデンクロス")
                score += 2

    if not signals:
        return None

    # 基本情報を計算
    current_price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2]) if len(close) >= 2 else current_price
    change_pct = ((current_price - prev_price) / prev_price * 100
                  if prev_price > 0 else 0.0)

    return {
        "ticker": ticker,
        "price": round(current_price, 1),
        "change_pct": round(change_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "signals": signals,
        "score": score,
        "data_date": str(ohlcv.index[-1].date())
        if hasattr(ohlcv.index[-1], "date") else str(ohlcv.index[-1]),
    }


def run_phase1(tickers: list[dict]) -> list[dict]:
    """Phase 1: 全銘柄をバッチダウンロードして粗いフィルターにかける。

    yf.download()で100銘柄ずつ一括取得し、各銘柄のシグナルを検出する。

    Returns:
        シグナルが検出された銘柄のリスト
    """
    logger.info("=== Phase 1: 粗いフィルター開始 (%d銘柄) ===", len(tickers))

    all_codes = [t["code"] for t in tickers]
    # 銘柄名の辞書
    name_map = {t["code"]: t["name"] for t in tickers}
    sector_map = {t["code"]: t.get("sector", "") for t in tickers}

    candidates = []
    failed_count = 0
    total_batches = (len(all_codes) + BATCH_SIZE - 1) // BATCH_SIZE

    iterator = range(0, len(all_codes), BATCH_SIZE)
    if HAS_TQDM:
        iterator = tqdm(
            iterator,
            total=total_batches,
            desc="Phase 1 バッチスキャン",
            unit="batch",
        )

    for batch_start in iterator:
        batch_codes = all_codes[batch_start:batch_start + BATCH_SIZE]
        batch_str = " ".join(batch_codes)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                data = yf.download(
                    batch_str,
                    period="3mo",
                    interval="1d",
                    progress=False,
                    threads=True,
                )
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "バッチ取得リトライ (%d/%d): %s",
                        attempt, MAX_RETRIES, e,
                    )
                    time.sleep(2 * attempt)
                else:
                    logger.error("バッチ取得失敗。スキップ: %s", e)
                    data = pd.DataFrame()

        if data.empty:
            failed_count += len(batch_codes)
            continue

        # 複数銘柄の場合、columnsはMultiIndex (指標, ティッカー)
        is_multi = isinstance(data.columns, pd.MultiIndex)

        for code in batch_codes:
            try:
                if is_multi:
                    # MultiIndexからこの銘柄のデータを抽出
                    if code not in data.columns.get_level_values(1):
                        failed_count += 1
                        continue
                    ticker_data = data.xs(code, level=1, axis=1).copy()
                else:
                    # 1銘柄だけの場合はそのまま
                    if len(batch_codes) == 1:
                        ticker_data = data.copy()
                    else:
                        failed_count += 1
                        continue

                ticker_data = ticker_data.dropna(how="all")
                if ticker_data.empty:
                    failed_count += 1
                    continue

                result = detect_signals_batch(ticker_data, code)
                if result is not None:
                    result["name"] = name_map.get(code, code)
                    result["sector"] = sector_map.get(code, "")
                    candidates.append(result)

            except Exception:
                failed_count += 1
                continue

    logger.info(
        "Phase 1 完了: %d銘柄中 %d銘柄がシグナル検出 (取得失敗: %d)",
        len(all_codes), len(candidates), failed_count,
    )
    return candidates


# ==============================================================
# Phase 2: 詳細分析
# ==============================================================

def analyze_single_ticker(candidate: dict) -> dict:
    """Phase 2: 1銘柄の詳細分析を行う。

    yfinanceのinfoから時価総額、PER、PBR、セクターを取得し、
    RSI、MACD、ボリンジャーバンドでスコアを追加する。
    """
    ticker_code = candidate["ticker"]

    # Phase 1のスコアをベースにする
    score = candidate["score"]
    detail = {
        "market_cap": None,
        "per": None,
        "pbr": None,
        "yf_sector": None,
        "rsi": None,
        "macd_hist": None,
        "bb_pct": None,
    }

    # info取得（重い処理なのでPhase 2のみ）
    try:
        tk = yf.Ticker(ticker_code)
        info = tk.info or {}
        detail["market_cap"] = info.get("marketCap")
        detail["per"] = info.get("trailingPE")
        detail["pbr"] = info.get("priceToBook")
        detail["yf_sector"] = info.get("sector")
    except Exception:
        pass

    # テクニカル指標を計算（改めてデータ取得）
    try:
        df = yf.download(ticker_code, period="3mo", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if not df.empty and len(df) >= 25:
            close = df["Close"]

            # RSI
            rsi = calc_rsi(close, 14)
            rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
            detail["rsi"] = round(rsi_val, 1) if rsi_val is not None else None

            if rsi_val is not None:
                if 30 <= rsi_val <= 70:
                    score += 1   # 適正範囲
                elif rsi_val < 30:
                    score += 2   # 売られすぎ反発

            # MACD
            macd_line, signal_line = calc_macd(close)
            if not pd.isna(macd_line.iloc[-1]) and not pd.isna(signal_line.iloc[-1]):
                macd_hist = float(macd_line.iloc[-1] - signal_line.iloc[-1])
                detail["macd_hist"] = round(macd_hist, 2)

            # ボリンジャーバンド
            _, _, _, pct_b = calc_bollinger(close)
            if not pd.isna(pct_b.iloc[-1]):
                detail["bb_pct"] = round(float(pct_b.iloc[-1]), 3)

    except Exception:
        pass

    candidate["score"] = score
    candidate["detail"] = detail
    return candidate


def run_phase2(candidates: list[dict]) -> list[dict]:
    """Phase 2: 候補銘柄の詳細分析を並列実行する。"""
    if not candidates:
        return []

    logger.info("=== Phase 2: 詳細分析開始 (%d銘柄) ===", len(candidates))

    results = []
    with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as executor:
        future_map = {
            executor.submit(analyze_single_ticker, c): c
            for c in candidates
        }

        iterator = as_completed(future_map)
        if HAS_TQDM:
            iterator = tqdm(
                iterator,
                total=len(future_map),
                desc="Phase 2 詳細分析",
                unit="銘柄",
            )

        for future in iterator:
            try:
                result = future.result(timeout=30)
                results.append(result)
            except Exception as e:
                original = future_map[future]
                logger.warning(
                    "詳細分析失敗: %s - %s",
                    original.get("ticker", "?"), e,
                )
                results.append(original)

    # スコア降順ソート
    results.sort(key=lambda x: x["score"], reverse=True)

    logger.info("Phase 2 完了: %d銘柄の詳細分析終了", len(results))
    return results


# ==============================================================
# 出力
# ==============================================================

def print_results_rich(results: list[dict], top_n: int = 20):
    """richテーブルで結果を表示する。"""
    console = Console()

    table = Table(
        title=f"東証全市場スキャン結果 [{datetime.now().strftime('%Y-%m-%d %H:%M')}]",
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("コード", style="cyan", width=8)
    table.add_column("銘柄名", width=16)
    table.add_column("終値", justify="right", width=10)
    table.add_column("前日比", justify="right", width=8)
    table.add_column("出来高倍率", justify="right", width=10)
    table.add_column("シグナル", width=28)
    table.add_column("スコア", justify="right", style="bold", width=6)
    table.add_column("RSI", justify="right", width=6)
    table.add_column("セクター", width=12)

    for i, r in enumerate(results[:top_n], 1):
        change_style = "green" if r["change_pct"] >= 0 else "red"
        score_style = "bold green" if r["score"] >= 7 else "bold yellow" if r["score"] >= 5 else ""

        detail = r.get("detail", {})
        rsi_str = f"{detail['rsi']:.0f}" if detail.get("rsi") is not None else "-"

        table.add_row(
            str(i),
            r["ticker"],
            r["name"][:14],
            f"{r['price']:,.1f}",
            f"[{change_style}]{r['change_pct']:+.1f}%[/{change_style}]",
            f"x{r['vol_ratio']:.1f}",
            ", ".join(r["signals"]),
            f"[{score_style}]{r['score']}[/{score_style}]" if score_style else str(r["score"]),
            rsi_str,
            r.get("sector", "")[:10],
        )

    console.print(table)
    console.print(f"\n  合計シグナル検出: {len(results)}銘柄 / 上位{min(top_n, len(results))}件を表示")


def print_results_plain(results: list[dict], top_n: int = 20):
    """プレーンテキストで結果を表示する。richが無い場合のフォールバック。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*90}")
    print(f"  東証全市場スキャン結果 [{now}]")
    print(f"{'='*90}")
    print(f"  {'#':<4} {'コード':<10} {'銘柄名':<16} {'終値':>10} {'前日比':>8} "
          f"{'出来高倍率':>10} {'スコア':>6} {'シグナル'}")
    print(f"  {'-'*4} {'-'*10} {'-'*16} {'-'*10} {'-'*8} {'-'*10} {'-'*6} {'-'*28}")

    for i, r in enumerate(results[:top_n], 1):
        signals_str = ", ".join(r["signals"])
        print(
            f"  {i:<4} {r['ticker']:<10} {r['name'][:14]:<16} "
            f"{r['price']:>10,.1f} {r['change_pct']:>+7.1f}% "
            f"{'x'}{r['vol_ratio']:<9.1f} {r['score']:>6} {signals_str}"
        )

    print(f"\n  合計シグナル検出: {len(results)}銘柄 / 上位{min(top_n, len(results))}件を表示")
    print()


def print_results(results: list[dict], top_n: int = 20):
    """結果を表示する。richがあればリッチ表示、なければプレーンテキスト。"""
    if HAS_RICH:
        print_results_rich(results, top_n)
    else:
        print_results_plain(results, top_n)


def save_results(results: list[dict]):
    """結果をJSONファイルに保存する。"""
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_signals": len(results),
        "results": results,
    }
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    logger.info("結果保存: %s", RESULT_FILE)


def notify_top_results(results: list[dict], top_n: int = 10):
    """上位銘柄をDiscord通知する。"""
    if not results:
        return

    top = results[:top_n]
    lines = []
    for r in top:
        signals_str = ", ".join(r["signals"])
        lines.append(
            f"- {r['name']}({r['ticker']}) "
            f"{r['price']:,.1f} ({r['change_pct']:+.1f}%) "
            f"スコア:{r['score']} [{signals_str}]"
        )

    send_discord_embed(
        title=f"東証全市場スキャン: {len(results)}銘柄検出",
        description="\n".join(lines),
        color=0x2ECC71,
        username="fullmarket-scanner",
    )


# ==============================================================
# メイン
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="東証全上場株スキャナー -- 急上昇シグナルを毎朝検出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "シグナル検出条件:\n"
            "  出来高急騰(+3)  直近出来高が20日平均の2倍以上\n"
            "  ブレイクアウト(+3)  終値が直近20日高値を更新\n"
            "  ギャップアップ(+2)  当日始値が前日終値比+2%以上\n"
            "  連続陽線(+1)  3日連続で終値 > 始値\n"
            "  ゴールデンクロス(+2)  5日EMAが25日EMAを上抜け\n"
            "  RSI適正範囲(+1)  RSI(14)が30-70\n"
            "  RSI売られすぎ(+2)  RSI(14)が30未満\n"
        ),
    )
    parser.add_argument(
        "--save", action="store_true",
        help="結果をfullmarket_scan_results.jsonに保存",
    )
    parser.add_argument(
        "--top", type=int, default=20,
        help="上位N件を表示（デフォルト: 20）",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="ティッカーリストをJPXから再取得",
    )
    parser.add_argument(
        "--min-score", type=int, default=0, dest="min_score",
        help="最低スコアフィルター（デフォルト: 0 = フィルターなし）",
    )
    args = parser.parse_args()

    start_time = time.time()

    # Step 1: ティッカーリスト取得
    tickers = load_ticker_list(refresh=args.refresh)
    if not tickers:
        logger.error("ティッカーリストが空です。--refresh で再取得してください。")
        sys.exit(1)

    # Step 2: Phase 1 -- 粗いフィルター
    candidates = run_phase1(tickers)

    # Step 3: Phase 2 -- 詳細分析
    results = run_phase2(candidates)

    # 最低スコアフィルター
    if args.min_score > 0:
        results = [r for r in results if r["score"] >= args.min_score]
        logger.info("スコア %d以上でフィルター: %d銘柄", args.min_score, len(results))

    elapsed = time.time() - start_time
    logger.info("全体所要時間: %.1f秒 (%.1f分)", elapsed, elapsed / 60)

    # Step 4: 出力
    print_results(results, top_n=args.top)

    if args.save:
        save_results(results)

    # Discord通知
    notify_top_results(results, top_n=10)


if __name__ == "__main__":
    main()

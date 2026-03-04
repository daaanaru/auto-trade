"""
銘柄スクリーナー: 出来高急増 + 価格上昇の銘柄を自動抽出する。

ロス・キャメロンの手法に基づき、以下の条件でフィルタリング:
- 出来高が前日比500%以上増加（5倍以上）
- 価格が前日比5%以上上昇

ccxtで取引所（Bybit/Binance）の全ティッカーを取得し、条件合致銘柄を返す。

使い方:
    python screener.py                      # Bybitでスクリーニング
    python screener.py --exchange binance    # Binanceで
    python screener.py --vol-ratio 3.0       # 出来高倍率を変更
    python screener.py --price-change 3.0    # 価格変化率を変更
"""

import argparse
import logging
from typing import Optional

logger = logging.getLogger("auto-trade.screener")


class VolumeScreener:
    """出来高急増銘柄のスクリーナー。"""

    def __init__(self, exchange: str = "bybit", quote: str = "USDT"):
        """
        Args:
            exchange: 取引所名（ccxtがサポートするもの）
            quote: 基軸通貨でフィルタ（例: USDT, BTC）
        """
        try:
            import ccxt
        except ImportError:
            raise ImportError("ccxt is not installed. Run: pip install ccxt")

        exchange_class = getattr(ccxt, exchange, None)
        if exchange_class is None:
            raise ValueError(f"Unknown exchange: {exchange}")
        self._exchange = exchange_class({"enableRateLimit": True})
        self._quote = quote

    def scan(
        self,
        vol_ratio_min: float = 5.0,
        price_change_min: float = 5.0,
        top_n: Optional[int] = 20,
    ) -> list:
        """出来高急増 + 価格上昇の銘柄を抽出する。

        Args:
            vol_ratio_min: 出来高の前日比最低倍率（デフォルト5倍 = 500%）
            price_change_min: 価格の前日比最低上昇率%（デフォルト5%）
            top_n: 上位N件を返す（Noneなら全件）

        Returns:
            銘柄情報のリスト（出来高倍率降順でソート）
        """
        logger.info(
            "Scanning %s for vol>%.0fx, price>%.1f%% (%s pairs)",
            self._exchange.id, vol_ratio_min, price_change_min, self._quote,
        )

        # 全ティッカーを取得（24時間データ含む）
        tickers = self._exchange.fetch_tickers()

        results = []
        for symbol, ticker in tickers.items():
            # 基軸通貨でフィルタ（"BTC/USDT" or "BTC/USDT:USDT" 両方対応）
            base_quote = symbol.split(":")[0] if ":" in symbol else symbol
            if not base_quote.endswith(f"/{self._quote}"):
                continue

            # 必要なデータが揃っているかチェック
            price_change_pct = ticker.get("percentage")
            base_volume = ticker.get("baseVolume")  # 当日出来高
            quote_volume = ticker.get("quoteVolume")
            last_price = ticker.get("last")

            if price_change_pct is None or base_volume is None or last_price is None:
                continue
            if base_volume <= 0 or last_price <= 0:
                continue

            # 前日出来高の推定: 24h出来高÷(1+変化率)は不正確なので、
            # ccxtのtickerには前日出来高がないため、OHLCVから2日分を取得して比較する
            # ただし全銘柄でOHLCV取得するとレートリミットに引っかかるので、
            # まず価格変化率でフィルタしてから出来高を詳細チェックする
            if price_change_pct < price_change_min:
                continue

            results.append({
                "symbol": symbol,
                "last_price": last_price,
                "price_change_pct": round(price_change_pct, 2),
                "base_volume": base_volume,
                "quote_volume": quote_volume,
            })

        # 出来高の詳細チェック（OHLCVから前日比を計算）
        checked_results = []
        for item in results:
            vol_ratio = self._check_volume_ratio(item["symbol"])
            if vol_ratio is None:
                continue
            if vol_ratio >= vol_ratio_min:
                item["vol_ratio"] = round(vol_ratio, 1)
                checked_results.append(item)

        # 出来高倍率の降順でソート
        checked_results.sort(key=lambda x: x["vol_ratio"], reverse=True)

        if top_n is not None:
            checked_results = checked_results[:top_n]

        logger.info("Found %d symbols matching criteria", len(checked_results))
        return checked_results

    def _check_volume_ratio(self, symbol: str) -> Optional[float]:
        """直近2日のOHLCVを取得し、出来高の前日比倍率を返す。"""
        try:
            ohlcv = self._exchange.fetch_ohlcv(symbol, timeframe="1d", limit=2)
            if len(ohlcv) < 2:
                return None
            vol_prev = ohlcv[-2][5]  # 前日出来高
            vol_curr = ohlcv[-1][5]  # 当日出来高
            if vol_prev <= 0:
                return None
            return vol_curr / vol_prev
        except Exception:
            return None


def main():
    parser = argparse.ArgumentParser(description="Volume Surge Screener")
    parser.add_argument("--exchange", default="bybit", help="Exchange (default: bybit)")
    parser.add_argument("--quote", default="USDT", help="Quote currency (default: USDT)")
    parser.add_argument("--vol-ratio", type=float, default=5.0,
                        help="Min volume ratio vs previous day (default: 5.0 = 500%%)")
    parser.add_argument("--price-change", type=float, default=5.0,
                        help="Min price change %% (default: 5.0)")
    parser.add_argument("--top", type=int, default=20, help="Max results (default: 20)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    screener = VolumeScreener(exchange=args.exchange, quote=args.quote)
    results = screener.scan(
        vol_ratio_min=args.vol_ratio,
        price_change_min=args.price_change,
        top_n=args.top,
    )

    if not results:
        print("\nNo symbols found matching criteria.")
        return

    print(f"\n{'Symbol':<16} {'Price':>12} {'Change%':>10} {'VolRatio':>10}")
    print("-" * 50)
    for r in results:
        print(
            f"{r['symbol']:<16} {r['last_price']:>12.4f} "
            f"{r['price_change_pct']:>+9.1f}% {r['vol_ratio']:>9.1f}x"
        )


if __name__ == "__main__":
    main()

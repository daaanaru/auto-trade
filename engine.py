"""
BacktestEngine: AI生成戦略のバックテストエンジン

- ルックアヘッドバイアス対策済み
- スリッページ・手数料のリアルなモデリング
- ウォークフォワード検証対応
- DataFetcher: YFinance / CCXT からOHLCVデータを取得
"""

import numpy as np
import pandas as pd
import yfinance as yf
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from plugins.strategies.base_strategy import BaseStrategy, BacktestResult


@dataclass
class BacktestConfig:
    """バックテスト設定"""
    initial_capital: float = 1_000_000      # 初期資金（円）
    commission_rate: float = 0.001          # 手数料 0.1%
    slippage_rate: float = 0.0005           # スリッページ 0.05%
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class BacktestEngine:
    """
    ベクトル化バックテストエンジン（高速）
    
    使い方:
        engine = BacktestEngine()
        result = engine.run(strategy, data)
        print(result.summary())
    """

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()

    def run(self, strategy: BaseStrategy, data: pd.DataFrame, verbose: bool = True) -> BacktestResult:
        """
        バックテストを実行する。
        
        Args:
            strategy: BaseStrategyを継承した戦略インスタンス
            data: OHLCV DataFrame（列: open, high, low, close, volume）
        """
        if verbose:
            print(f"\n📈 バックテスト開始: {strategy.meta.name}")
            print(f"   期間: {data.index[0].date()} ～ {data.index[-1].date()}")
            print(f"   データ数: {len(data)}本")

        # データ期間フィルタ
        if self.config.start_date:
            data = data[data.index >= self.config.start_date]
        if self.config.end_date:
            data = data[data.index <= self.config.end_date]

        # シグナル生成（ルックアヘッドバイアス対策: shift(1)で前日シグナルを使用）
        signals = strategy.generate_signals(data)
        signals = signals.shift(1).fillna(0)  # 翌日始値で執行

        # ポジション変化を検出
        position = signals.copy()

        # 損益計算
        close = data["close"]
        returns = close.pct_change().fillna(0)

        # 手数料・スリッページの適用
        trades = position.diff().abs()  # ポジション変化 = トレード発生
        costs = trades * (self.config.commission_rate + self.config.slippage_rate)

        # ポートフォリオリターン
        strategy_returns = (position * returns) - costs

        # 資産曲線
        equity_curve = (1 + strategy_returns).cumprod() * self.config.initial_capital

        # 統計計算
        annual_return = self._calc_annual_return(strategy_returns)
        max_drawdown = self._calc_max_drawdown(equity_curve)
        sharpe = self._calc_sharpe(strategy_returns)
        win_rate, total_trades = self._calc_win_rate(strategy_returns, position)

        period = f"{data.index[0].date()} ～ {data.index[-1].date()}"

        result = BacktestResult(
            annual_return=annual_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            total_trades=total_trades,
            period=period,
            equity_curve=equity_curve,
        )

        if verbose:
            print(result.summary())

        return result

    def walk_forward(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        train_months: int = 12,
        test_months: int = 3,
        verbose: bool = True,
    ) -> list[BacktestResult]:
        """
        ウォークフォワード検証（過学習防止）
        train_monthsで学習、test_monthsで検証を時系列でローリング
        """
        results = []
        start = data.index[0]
        end = data.index[-1]

        current = start + pd.DateOffset(months=train_months)
        fold = 1

        while current + pd.DateOffset(months=test_months) <= end:
            test_end = current + pd.DateOffset(months=test_months)
            test_data = data[current:test_end]

            if verbose:
                print(f"\n🔄 ウォークフォワード Fold {fold}: {current.date()} ～ {test_end.date()}")

            result = self.run(strategy, test_data, verbose=False)
            results.append(result)

            current = test_end
            fold += 1

        if verbose and results:
            avg_return = np.mean([r.annual_return for r in results])
            avg_sharpe = np.mean([r.sharpe_ratio for r in results])
            print(f"\n📊 ウォークフォワード集計 ({len(results)}フォールド)")
            print(f"   平均年率リターン: {avg_return:+.1f}%")
            print(f"   平均シャープレシオ: {avg_sharpe:.2f}")

        return results

    # ----------------------------------------------------------
    # 統計計算ヘルパー
    # ----------------------------------------------------------

    def _calc_annual_return(self, returns: pd.Series) -> float:
        """年率リターン（%）"""
        total = (1 + returns).prod()
        years = len(returns) / 252
        if years == 0:
            return 0.0
        return ((total ** (1 / years)) - 1) * 100

    def _calc_max_drawdown(self, equity: pd.Series) -> float:
        """最大ドローダウン（%）"""
        peak = equity.cummax()
        dd = (equity - peak) / peak
        return dd.min() * 100

    def _calc_sharpe(self, returns: pd.Series, risk_free: float = 0.01) -> float:
        """シャープレシオ（年率）"""
        excess = returns - risk_free / 252
        if returns.std() == 0:
            return 0.0
        return (excess.mean() / returns.std()) * np.sqrt(252)

    def _calc_win_rate(self, returns: pd.Series, position: pd.Series) -> tuple:
        """勝率（%）と総トレード数"""
        trade_returns = returns[position.diff().abs() > 0]
        if len(trade_returns) == 0:
            return 0.0, 0
        win_rate = (trade_returns > 0).sum() / len(trade_returns) * 100
        return win_rate, len(trade_returns)


# ==============================================================
# DataFetcher: データ取得の抽象化レイヤー
# ==============================================================

class BaseDataFetcher(ABC):
    """データ取得の基底クラス。"""

    @abstractmethod
    def fetch(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        """OHLCVデータを取得して返す。

        Returns:
            columns: open, high, low, close, volume（全て小文字）
            index: DatetimeIndex
        """
        pass


class YFinanceFetcher(BaseDataFetcher):
    """Yahoo Finance からデータを取得する。

    対応: 株式（AAPL, 7203.T）、暗号資産（BTC-USD）、ETF等。
    """

    def fetch(self, symbol: str = "BTC-USD", period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        print(f"[YFinance] Fetching {symbol} ({period}, {interval})...")
        df = yf.download(symbol, period=period, interval=interval)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.dropna()
        return df


class CCXTFetcher(BaseDataFetcher):
    """ccxt ライブラリを使い、暗号資産取引所からOHLCVを取得する。

    対応取引所: Bybit, Binance 等（ccxtがサポートする全取引所）。
    APIキー不要（公開データのみ取得）。

    使い方:
        fetcher = CCXTFetcher(exchange="bybit")
        data = fetcher.fetch("BTC/USDT", period="90d")
    """

    # period文字列をミリ秒に変換するためのマッピング
    _PERIOD_DAYS = {
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "90d": 90,
        "180d": 180,
        "365d": 365,
    }

    # interval文字列をccxt timeframeに変換
    _INTERVAL_MAP = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
        "1w": "1w",
    }

    def __init__(self, exchange: str = "bybit"):
        try:
            import ccxt
        except ImportError:
            raise ImportError("ccxt is not installed. Run: pip install ccxt")

        exchange_class = getattr(ccxt, exchange, None)
        if exchange_class is None:
            raise ValueError(f"Unknown exchange: {exchange}. See ccxt.exchanges for available.")
        self._exchange = exchange_class({"enableRateLimit": True})

    def fetch(self, symbol: str = "BTC/USDT", period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        import ccxt
        from datetime import datetime, timedelta

        timeframe = self._INTERVAL_MAP.get(interval, "1d")
        days = self._PERIOD_DAYS.get(period, 365)
        since_dt = datetime.utcnow() - timedelta(days=days)
        since_ms = int(since_dt.timestamp() * 1000)

        print(f"[CCXT/{self._exchange.id}] Fetching {symbol} ({period}, {timeframe})...")

        all_ohlcv = []
        fetch_since = since_ms
        limit = 1000  # 1回のリクエストで取得する最大本数

        while True:
            ohlcv = self._exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=fetch_since, limit=limit
            )
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            # 最後のタイムスタンプの次から再取得
            fetch_since = ohlcv[-1][0] + 1
            if len(ohlcv) < limit:
                break  # 全件取得完了

        if not all_ohlcv:
            raise ValueError(f"No data returned for {symbol} on {self._exchange.id}")

        df = pd.DataFrame(
            all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp")
        df = df.dropna()
        print(f"  -> {len(df)} bars fetched")
        return df

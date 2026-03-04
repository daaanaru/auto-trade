"""
trade_engine.py — トレードエンジン基底クラス

PaperTradeEngine と LiveTradeEngine の共通インターフェースを定義する。
両エンジンはこのクラスを継承し、同じメソッド名で操作できる。

使い方:
    from trade_engine import TradeEngine

    # ペーパートレード
    from paper_trade import PaperTradeEngine
    engine = PaperTradeEngine(strategy_key="vol_div")

    # 実弾トレード（同じインターフェース）
    from live_trade import LiveTradeEngine
    engine = LiveTradeEngine(strategy_key="vol_div")

    # 共通操作
    engine.run()              # シグナル判定 → 売買実行
    engine.get_position()     # 現在ポジション
    engine.get_balance()      # 残高
    engine.emergency_close()  # 全ポジション強制決済
    engine.status()           # ステータス表示
"""

from abc import ABC, abstractmethod
from pathlib import Path


class TradeEngine(ABC):
    """トレードエンジンの基底クラス。

    PaperTradeEngine と LiveTradeEngine が継承する。
    同じメソッド名で操作でき、切り替えが容易。
    """

    def __init__(self, strategy_key: str = "vol_div"):
        self.strategy_key = strategy_key

    @abstractmethod
    def run(self) -> dict:
        """シグナル判定 → 売買実行のメインループ。

        Returns:
            実行後の状態辞書
        """

    @abstractmethod
    def get_position(self) -> dict:
        """現在のポジション情報を返す。

        Returns:
            {"position": float, "entry_price": float, "direction": str}
        """

    @abstractmethod
    def get_balance(self) -> dict:
        """残高情報を返す。

        Returns:
            {"capital": float, "total_pnl": float, ...}
        """

    @abstractmethod
    def market_buy(self, amount: float) -> dict:
        """成行買い。

        Args:
            amount: BTC数量

        Returns:
            注文結果の辞書
        """

    @abstractmethod
    def market_sell(self, amount: float) -> dict:
        """成行売り。

        Args:
            amount: BTC数量

        Returns:
            注文結果の辞書
        """

    @abstractmethod
    def emergency_close(self) -> dict:
        """全ポジションを強制決済する。

        Returns:
            実行後の状態辞書
        """

    @abstractmethod
    def status(self):
        """ステータスを表示する。"""

    @abstractmethod
    def reset(self):
        """状態をリセットして初期化する。"""

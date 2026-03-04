"""
BaseStrategy: AIが生成する全戦略の規約（インターフェース）
この規約に従っていれば、どんな戦略でもプラットフォームで動く
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd


@dataclass
class StrategyMeta:
    """戦略のメタ情報（AIが自動入力）"""
    name: str
    market: str                    # crypto / jp_stock / us_stock / fx
    version: str = "1.0.0"
    created_by: str = "AI"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    origin_prompt: str = ""        # 元になった指示文
    description: str = ""
    tags: list = field(default_factory=list)


@dataclass
class BacktestResult:
    """バックテスト結果"""
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    period: str
    equity_curve: Optional[pd.Series] = None
    trade_log: Optional[pd.DataFrame] = None

    def summary(self) -> str:
        return (
            f"📊 バックテスト結果\n"
            f"  年率リターン  : {self.annual_return:+.1f}%\n"
            f"  最大ドローダウン: {self.max_drawdown:.1f}%\n"
            f"  シャープレシオ : {self.sharpe_ratio:.2f}\n"
            f"  勝率          : {self.win_rate:.1f}%\n"
            f"  総トレード数  : {self.total_trades}回\n"
            f"  期間          : {self.period}"
        )


class BaseStrategy(ABC):
    """
    全戦略の基底クラス。
    AIはこのクラスを継承したコードを生成する。

    必須実装:
        - generate_signals()
        - position_size()

    自動で使える機能:
        - backtest()
        - validate()
        - to_dict()
    """

    def __init__(self, meta: StrategyMeta, params: dict = None):
        self.meta = meta
        self.params = params or {}
        self.status = "draft"   # draft → backtesting → paper → live → retired

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        売買シグナルを生成する。
        Returns: pd.Series with values: 1(買), -1(売), 0(様子見)
        """
        pass

    @abstractmethod
    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        """
        ポジションサイズ（株数・枚数）を返す。
        """
        pass

    def risk_check(self, order: dict) -> tuple[bool, str]:
        """
        共通リスクチェック。サブクラスでオーバーライド可能。
        Returns: (通過可否, 理由)
        """
        # 最大ポジションサイズチェック（デフォルト: ポートフォリオの20%以内）
        max_position_pct = self.params.get("max_position_pct", 0.20)
        if order.get("position_pct", 0) > max_position_pct:
            return False, f"ポジションサイズ超過: {order['position_pct']:.1%} > {max_position_pct:.1%}"
        return True, "OK"

    def validate(self) -> tuple[bool, list[str]]:
        """戦略コードの最低限の整合性チェック"""
        errors = []
        if not self.meta.name:
            errors.append("戦略名が未設定")
        if self.meta.market not in ["crypto", "jp_stock", "us_stock", "fx"]:
            errors.append(f"不明な市場: {self.meta.market}")
        return len(errors) == 0, errors

    def to_dict(self) -> dict:
        """YAML登録用の辞書化"""
        return {
            "name": self.meta.name,
            "market": self.meta.market,
            "version": self.meta.version,
            "created_by": self.meta.created_by,
            "created_at": self.meta.created_at,
            "origin_prompt": self.meta.origin_prompt,
            "description": self.meta.description,
            "tags": self.meta.tags,
            "params": self.params,
            "status": self.status,
        }

    def __repr__(self):
        return f"<Strategy: {self.meta.name} | {self.meta.market} | {self.status}>"

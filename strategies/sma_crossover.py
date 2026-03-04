import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta

class SMACrossoverStrategy(BaseStrategy):
    """
    SMAクロスオーバー戦略
    短期SMAが長期SMAを上抜けたら買い、下抜けたら売り
    """
    
    def __init__(self, params: dict = None):
        meta = StrategyMeta(
            name="SMA_Crossover",
            market="crypto",
            version="1.0.0",
            description="Simple Moving Average Crossover Strategy",
            tags=["trend", "sma"]
        )
        default_params = {
            "sma_short": 5,
            "sma_long": 20,
            "risk_per_trade": 0.05
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)
    
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=data.index)
        short_sma = data['close'].rolling(window=self.params['sma_short']).mean()
        long_sma = data['close'].rolling(window=self.params['sma_long']).mean()
        
        # ゴールデンクロス (1) / デッドクロス (-1)
        signals[short_sma > long_sma] = 1
        signals[short_sma < long_sma] = -1
        
        return signals
    
    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

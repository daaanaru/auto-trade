import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta

class RSIMeanReversionStrategy(BaseStrategy):
    """
    RSI逆張り戦略
    RSIが30以下で買い、70以上で売り
    """
    
    def __init__(self, params: dict = None):
        meta = StrategyMeta(
            name="RSI_Mean_Reversion",
            market="crypto",
            version="1.0.0",
            description="RSI Overbought/Oversold Strategy",
            tags=["mean_reversion", "rsi"]
        )
        default_params = {
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "risk_per_trade": 0.05
        }
        if params:
            default_params.update(params)
        super().__init__(meta=meta, params=default_params)
    
    def _calculate_rsi(self, series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=data.index)
        rsi = self._calculate_rsi(data['close'], self.params['rsi_period'])
        
        signals[rsi < self.params['rsi_oversold']] = 1
        signals[rsi > self.params['rsi_overbought']] = -1
        
        return signals
    
    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        risk_pct = self.params.get("risk_per_trade", 0.05)
        return (portfolio_value * risk_pct) / price

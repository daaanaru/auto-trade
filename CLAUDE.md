# auto-trade — マルチマーケット自動売買システム

> 株・暗号資産の自動売買戦略を検証し、段階的に実弾投入する実験。
> バックテスト → WF検証 → ペーパートレード → 少額実弾の4段階で進む。

---

## 現在地

- バックテストエンジン（engine.py）完成
- yfinance + ccxt の2系統データ取得（DataFetcher抽象化）
- 特徴量エンジン（FeatureEngine）でRSI/MACD/BB/ATR/OBV/ADX/MFI/EMA/VO/VWAP一括計算
- 戦略7本: SMAクロスオーバー / RSI平均回帰 / BB+RSIコンボ / 月初モメンタム / 出来高ダイバージェンス / モメンタムプルバック / オーダーブロック
- **Optunaパラメータ最適化完了**（6/7戦略がプラス転換）
- **マルチマーケット検証完了**: 日本株/米国株/BTC/FX
- **ウォークフォワード検証完了**: Sony x Monthly Momentum WF Sharpe 1.99（最高評価）
- **日経225全自動スクリーナー稼働中**: 信越化学・日本電産をwatchlistに自動追加
- **ペーパートレード環境稼働中**: BTC/JPY（paper_trade.py + crypto_monitor.py）
- **実弾投入レイヤー準備完了**: bitFlyer ccxt経由（live_trade.py、DRY_RUNデフォルト）
- **TradeEngine基底クラス**: Paper/Liveを同一インターフェースで切り替え可能

### 対応市場

| 市場 | データソース | 銘柄例 | 有望戦略 |
|------|-----------|--------|---------|
| 日本株 | yfinance | 6758.T, 9984.T, 7974.T, 4063.T | **Monthly Momentum（WF合格）** |
| 米国株 | yfinance | AAPL, NVDA, SPY | BB+RSI Combo |
| BTC | yfinance / ccxt (Bybit) | BTC-USD, BTC/USDT | Volume Divergence |
| FX | yfinance | USDJPY=X | 全戦略不適合 |

### 主要ファイル

| ファイル | 役割 |
|---------|------|
| engine.py | バックテストエンジン + DataFetcher（YFinance/CCXT） |
| trade_engine.py | TradeEngine基底クラス（Paper/Live共通インターフェース） |
| optimize.py | Optunaパラメータ最適化 + ウォークフォワード検証 |
| signal_monitor.py | シグナル監視（単体/ウォッチリスト一括） |
| paper_trade.py | ペーパートレード（BTC/JPY仮想売買） |
| live_trade.py | 実弾トレード（bitFlyer ccxt経由、DRY_RUNデフォルト） |
| crypto_monitor.py | 仮想通貨自律監視（Ollama LLM分析 + 卒業条件チェック） |
| jp_stock_screener.py | 日経225全自動スクリーニング + WF検証 |
| screener.py | 汎用銘柄スクリーナー |
| run_backtest.py | 単一銘柄×全7戦略の一括バックテスト |
| run_multi_market.py | マルチマーケット一括バックテスト |
| run_timeframe_backtest.py | 短期足（1h/5m）バックテスト |
| cli.py | 対話型CLI（Claude API戦略生成） |
| monitoring_agent.py | 市場監視 + Discord通知 |
| watchlist.json | シグナル監視対象銘柄（WF合格銘柄を登録） |
| optimized_params.json | Optuna最適化済みパラメータ |
| crypto_config.json | 仮想通貨監視設定 |
| EXPERIMENTS.md | 全検証結果の総合レポート |
| FX_TRADING_RESEARCH.md | FX自動売買の調査結果 |

### 卒業条件（ペーパートレード → 実弾投入）

| 条件 | 閾値 |
|------|------|
| ペーパートレード期間 | 最低2週間 |
| 勝率 | 40%以上 |
| ローリングSharpe | 0.5以上 |
| 最大ドローダウン | -15%以内 |
| バックテスト結果との乖離 | +-20%以内 |

`python3 crypto_monitor.py --status` で確認可能。

## 山頂

- 卒業条件をクリアした戦略で少額実弾投入
- 複数市場×複数戦略のポートフォリオ運用

## 次の一歩

1. ~~パラメータ最適化~~ → **完了**
2. ~~マルチマーケット検証~~ → **完了**
3. ~~ウォークフォワード検証~~ → **完了**
4. ~~シグナル監視（signal_monitor.py）~~ → **完了**
5. ~~ペーパートレード環境~~ → **完了**（paper_trade.py + crypto_monitor.py）
6. ~~日経225全自動スクリーナー~~ → **完了**（jp_stock_screener.py）
7. ~~実弾投入レイヤー~~ → **完了**（live_trade.py、DRY_RUNモード）
8. **ペーパートレード実績の蓄積**（最低2週間）→ 卒業条件チェック
9. **DRY_RUNテスト** → 実弾投入判断

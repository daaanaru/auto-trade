# auto-trade — マルチマーケット自動売買システム

> 株・暗号資産・ゴールドの自動売買戦略を検証し、段階的に実弾投入する実験。
> バックテスト → WF検証 → ペーパートレード → 少額実弾の4段階で進む。

---

## 現在地

- バックテストエンジン（engine.py）完成
- yfinance + ccxt の2系統データ取得（DataFetcher抽象化）
- 特徴量エンジン（FeatureEngine）でRSI/MACD/BB/ATR/OBV/ADX/MFI/EMA/VO/VWAP一括計算
- 戦略7本: SMAクロスオーバー / RSI平均回帰 / BB+RSIコンボ / 月初モメンタム / 出来高ダイバージェンス / モメンタムプルバック / オーダーブロック
- **Optunaパラメータ最適化完了**（6/7戦略がプラス転換）
- **マルチマーケット検証完了**: 日本株/米国株/BTC/ゴールド
- **ウォークフォワード検証完了**: Sony x Monthly Momentum WF Sharpe 1.99（最高評価）
- **全市場統合スクリーナー稼働中**: 日本株/米国株/BTC/ゴールドを毎朝6:00に一括スキャン
- **$200ペーパートレード稼働中**: 全市場対応（テクニカル+ファンダメンタル分析）、3/30期限
- **ショート+レバレッジ対応済み（3/10追加）**: SELLシグナル+ファンダスコア<0でショートエントリー、レバレッジ2倍
- **自動卒業判定**: graduation_checker.py で毎日9:30に条件チェック
- **統合ダッシュボード**: dashboard.py で全市場の状況を一覧表示
- **実弾投入レイヤー準備完了**: bitFlyer ccxt経由（live_trade.py、DRY_RUNデフォルト）

### 対応市場

| 市場 | データソース | 銘柄例 | 有望戦略 |
|------|-----------|--------|---------|
| 日本株 | yfinance | 6758.T, 9984.T, 7974.T, 4063.T | **Monthly Momentum（WF合格）** |
| 米国株 | yfinance | AAPL, NVDA, GOOGL, JPM, LLY 他50銘柄 | BB+RSI Combo |
| BTC | yfinance / ccxt (Bybit) | BTC-USD, BTC/USDT | Volume Divergence |
| ゴールド | yfinance | GLD, GC=F | BB+RSI Combo |
| FX | yfinance | EURUSD=X, USDJPY=X, GBPUSD=X 他10ペア | **BB+RSI Combo（3/12対応開始・上様指示）** |

### 主要ファイル

| ファイル | 役割 |
|---------|------|
| **unified_screener.py** | **全市場統合スクリーナー（日本株/米国株/BTC/ゴールド）** |
| **unified_paper_trade.py** | **全市場統合ペーパートレーダー（$200, テクニカル+ファンダ）** |
| **graduation_checker.py** | **自動卒業判定ツール** |
| **dashboard.py** | **統合ダッシュボード（rich表示）** |
| engine.py | バックテストエンジン + DataFetcher（YFinance/CCXT） |
| trade_engine.py | TradeEngine基底クラス（Paper/Live共通インターフェース） |
| optimize.py | Optunaパラメータ最適化 + ウォークフォワード検証 |
| signal_monitor.py | シグナル監視（単体/ウォッチリスト一括） |
| paper_trade.py | ペーパートレード（BTC/JPY仮想売買） |
| live_trade.py | 実弾トレード（bitFlyer ccxt経由、DRY_RUNデフォルト） |
| crypto_monitor.py | 仮想通貨自律監視（Ollama LLM分析 + 卒業条件チェック） |
| jp_stock_screener.py | 日経225全自動スクリーニング + WF検証 |
| us_stock_tickers.json | S&P500主要50銘柄リスト + ゴールドティッカー |
| fx_tickers.json | FXティッカーリスト（3/12追加） |
| market_hours.py | 市場時間フィルター（3/12追加。閉場中のAPI呼び出し抑制） |
| paper_portfolio_v1_backup.json | リセット前のポートフォリオバックアップ |
| paper_portfolio.json | $200ペーパートレードのポートフォリオ状態 |
| daily_report.md | 日次損益レポート（自動生成） |
| watchlist.json | シグナル監視対象銘柄（WF合格銘柄を登録） |
| optimized_params.json | Optuna最適化済みパラメータ |
| crypto_config.json | 仮想通貨監視設定 |
| llm_ab_tracker.py | LLM A/Bテスト。シグナルのみ vs Ollama qwen2.5:7b のトレード判断を並行記録 |

### $200ペーパートレード体制（3/30期限）

| 項目 | 設定 |
|------|------|
| 初期資金 | 30,000 JPY ($200) |
| レバレッジ | 2倍（購買力 = 現金 x 2） |
| 1銘柄上限 | ポートフォリオの20%（レバレッジ後の実効値で計算） |
| 同時保有上限 | 5ポジション（ロング+ショート合計） |
| 損切り | -3%で全決済（ロング/ショート共通） |
| 利確第1段階 | +5%で1/3利確 |
| 利確第2段階 | +10%でさらに1/3利確 |
| トレーリングストップ | 第1利確後、高値(安値)から-3%(+3%)で残り全決済 |
| ロングエントリー | テクニカルBUY + ファンダスコア >= 0.1 |
| ショートエントリー | テクニカルSELL + ファンダスコア < 0（業績悪い銘柄を空売り） |
| 同一銘柄制限 | ロング+ショート同時保有禁止 |
| 判断基準 | チャート面（7戦略）+ ファンダメンタル面（PER/PBR/配当/売上成長） |

### 卒業条件（ペーパートレード → 実弾投入）

| 条件 | 閾値 |
|------|------|
| ペーパートレード期間 | 最低2週間 |
| 勝率 | 40%以上 |
| ローリングSharpe | 0.5以上 |
| 最大ドローダウン | -15%以内 |
| バックテスト結果との乖離 | +-20%以内 |

確認コマンド: `python3 graduation_checker.py` / `python3 dashboard.py`

### launchdジョブ一覧

| ジョブ | 実行時間 | スクリプト |
|--------|---------|-----------|
| **unified-screener** | **6回/日（0,4,8,12,16,20時）** | unified_screener.py --save |
| **unified-paper-trade** | **6回/日（1,5,9,13,17,21時）** | unified_paper_trade.py |
| **graduation-checker** | **2回/日（9:30, 21:30）** | graduation_checker.py |
| signal-monitor | 毎朝8:50 | signal_monitor.py --watchlist |
| paper-trade | 毎日9:00 | paper_trade.py |
| crypto-monitor | 毎時 | crypto_monitor.py |
| crypto-full-report | 毎日9:00 | crypto_monitor.py --report |
| jp_stock_screener | 毎週日曜深夜 | jp_stock_screener.py |
| **jp-fullmarket-scanner** | **毎朝8:00** | **jp_fullmarket_scanner.py --save --min-score 5（東証全3,615銘柄）** |

### 通知連携

| チャネル | 状態 | 設定 |
|---------|------|------|
| Discord Webhook | **設定済み・稼働中** | notifier.py → BUY/SELL/卒業判定を通知 |
| Google Calendar | MCP経由で設定済み | 3/9〜3/30 毎朝7:00 巡回リマインダー |

## 山頂

- $200ペーパートレードで3/30まで運用し、利益を出す
- 卒業条件をクリアした戦略で少額実弾投入
- 複数市場×複数戦略のポートフォリオ運用

## 次の一歩

1. **ドローダウンリセット実行**（上様承認待ち。3/16期限。虚偽の-34.23%を除去しないと卒業不可能が確定）
2. **Ollama停止判断**（A群37% vs B群22%、81件。上様承認待ち。停止でメモリ8.2GB解放）
3. **卒業期限延長**（3/30→4/12前後を推奨。上様承認待ち。延長しないと評価期間が4日のみ）
4. **経過報告・戦略改善**（残り17日で卒業条件クリアを目指す。決済0件が最大の課題）
5. **BTC戦略の方針決定** → 上様に3択確認（卒業条件からBTC分離/戦略追加/放置）
6. **卒業条件クリア → DRY_RUNテスト → 実弾投入判断**

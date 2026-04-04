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
- **全市場統合スクリーナー稼働中**: 日本株/米国株/BTC/ゴールド/FXを2時間ごとにスキャン（市場時間フィルター付き）
- **30万円ペーパートレード稼働中（3/13リセット、4/12期限・残11日）**: 5市場対応。**現在5ポジション（ETH/三菱商事/JT/三菱UFJ/BTC）・現金248,259円・累計139件決済(勝率52.5%)**。bb_rsi停止+VolScale枠拡大パッケージは上様承認待ち18日目。最優秀戦略: vol_div（勝率100%、+1,728円）
- **VolScale戦略（動的SMA）稼働中（3/15開始、3/16アルトコインWF検証完了）**: ボラ応じたSMA期間伸縮。**新規エントリーはBTC+ETHのみ**（XRP/XLMはWF不合格→既存ポジション自然決済中）。仮想通貨決済8件(+1,205円)の主力源泉。ETH WF Sharpe 0.52（合格）
- **市場別ファンダメンタル分析（3/13追加）**: 株:PER/PBR, FX:DXY/金利差, BTC:半減期サイクル, Gold:VIX/実質金利
- **ショート+レバレッジ対応済み（3/10追加）**: SELLシグナル+ファンダスコア<0でショートエントリー、レバレッジ2倍
- **ポジション監視15分ごと（3/12追加）**: --monitorモードでSL/TP/強制決済(7日)/早期トレーリングを常時監視
- **自動卒業判定**: graduation_checker.py で毎日9:30に条件チェック
- **統合ダッシュボード**: dashboard.py で全市場の状況を一覧表示
- **戦略別パフォーマンス分析（4/1追加）**: strategy_attribution.py で戦略別集計・市場マトリクス・モメンタム比較・最悪TOP5を自動生成（テスト36件全パス）
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
| **unified_screener.py** | **全市場統合スクリーナー（日本株/米国株/BTC/ゴールド/FX）** |
| **unified_paper_trade.py** | **全市場統合ペーパートレーダー（30万円, テクニカル+市場別ファンダ）** |
| **graduation_checker.py** | **自動卒業判定ツール** |
| **graduation_simulator.py** | **卒業軌道シミュレーション（モンテカルロ1000回、戦略別貢献度、延長/緩和シナリオ比較）** |
| **dashboard.py** | **統合ダッシュボード（rich表示）** |
| **market_fundamental.py** | **市場別ファンダメンタル分析（株:PER/PBR, FX:DXY/金利差, BTC:半減期, Gold:VIX/実質金利）** |
| **market_hours.py** | **市場時間フィルター（閉場中のスキャン抑制）** |
| engine.py | バックテストエンジン + DataFetcher（YFinance/CCXT） |
| trade_engine.py | TradeEngine基底クラス（Paper/Live共通インターフェース） |
| optimize.py | Optunaパラメータ最適化 + ウォークフォワード検証 |
| signal_monitor.py | シグナル監視（単体/ウォッチリスト一括） |
| paper_trade.py | ペーパートレード（BTC/JPY仮想売買） |
| live_trade.py | 実弾トレード（bitFlyer ccxt経由、DRY_RUNデフォルト） |
| crypto_monitor.py | 仮想通貨自律監視（Ollama LLM分析 + 卒業条件チェック） |
| jp_stock_screener.py | 日経225全自動スクリーニング + WF検証 |
| us_stock_tickers.json | S&P500主要50銘柄リスト + ゴールドティッカー |
| fx_tickers.json | FXティッカーリスト（10ペア） |
| paper_portfolio.json | 30万円ペーパートレードのポートフォリオ状態 |
| trade_history.json | 全トレード履歴（永続記録） |
| daily_report.md | 日次損益レポート（自動生成） |
| watchlist.json | シグナル監視対象銘柄（WF合格銘柄を登録） |
| optimized_params.json | Optuna最適化済みパラメータ |
| crypto_config.json | 仮想通貨監視設定 |
| llm_ab_tracker.py | LLM A/Bテスト。シグナルのみ vs Ollama qwen2.5:7b のトレード判断を並行記録 |

### 30万円ペーパートレード体制（3/13リセット、4/12期限）

| 項目 | 設定 |
|------|------|
| 初期資金 | 300,000 JPY |
| レバレッジ | 2倍（購買力 = 現金 x 2） |
| 1銘柄上限 | ポートフォリオの4%（50枠に合わせて分散） |
| 同時保有上限 | 50ポジション（各市場10枠 x 5市場） |
| 損切り | ATR×1.5倍（キャップ-5%、下限-1.5%）。フォールバック-3%。（3/16改善: 旧キャップ-8%） |
| 利確第1段階 | ATR×3.0倍（下限+3%）で1/2利確。（3/16改善: 旧2.0倍/下限+2%） |
| 利確第2段階 | ATR×5.0倍（下限+6%）でさらに1/2利確 |
| トレーリングストップ | +3%到達で発動、高値(安値)から-1.5%で全決済。（3/16改善: 旧+2%発動/-2%決済） |
| 強制決済 | 7日経過で自動決済（塩漬け防止） |
| ロングエントリー | テクニカルBUY + 市場別ファンダスコア >= 0.1 |
| ショートエントリー | テクニカルSELL + 市場別ファンダスコア < 0 |
| 同一銘柄制限 | ロング+ショート同時保有禁止 |
| 判断基準 | チャート面（7戦略）+ 市場別ファンダメンタル面（株:PER/PBR, FX:DXY/金利差, BTC:半減期, Gold:VIX） |

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
| **unified-screener** | **12回/日（2時間ごと :00）** | unified_screener.py --save |
| **unified-paper-trade** | **12回/日（2時間ごと :30）** | unified_paper_trade.py |
| **position-monitor** | **15分ごと** | unified_paper_trade.py --monitor（SL/TP監視のみ） |
| **graduation-checker** | **2回/日（9:30, 21:30）** | graduation_checker.py |
| signal-monitor | 毎朝8:50 | signal_monitor.py --watchlist |
| paper-trade | 毎日9:00 | paper_trade.py |
| crypto-monitor | 毎時 | crypto_monitor.py |
| crypto-full-report | 毎日9:00 | crypto_monitor.py --report |
| jp_stock_screener | 毎週日曜深夜 | jp_stock_screener.py |
| **jp-fullmarket-scanner** | **毎朝6:00** | **jp_fullmarket_scanner.py --save --min-score 5（東証全3,615銘柄）** |

### 通知連携

| チャネル | 状態 | 設定 |
|---------|------|------|
| Discord Webhook | **設定済み・稼働中** | notifier.py → BUY/SELL/卒業判定を通知 |
| Google Calendar | MCP経由で設定済み | 3/9〜3/30 毎朝7:00 巡回リマインダー |

## 山頂

- 30万円ペーパートレードで4/12まで運用し、利益を出す
- 卒業条件をクリアした戦略で少額実弾投入
- 5市場×複数戦略のポートフォリオ運用

## 次の一歩（4/4 04:32更新 — 残8日で決着フェーズ）

### CRITICAL — 上様判断待ち（火急度順）
1. **卒業期限延長判断（4/5期限・明日までに決断必須）** — Sharpe -3.20→要0.5+。残8日で数学的に達成困難。**4/5までに判断が必須**
   - 選択肢: A)4/26延長（推奨・確率89.6%） B)条件緩和(Sharpe→0.0・78.3%) C)戦略別卒業
   - 外部要因注記: 中東紛争激化（2026-04-03〜）でBTC/ETH等リスク資産が急落。Extreme Fear。外部ショック期間として延長根拠に加算できる
2. **bb_rsi停止承認（上様承認待ち23日目・火急）** — 損失の76%がbb_rsi由来。config.json変更の承認要
3. ~~**jquants-crawler .env修正（上様操作要）**~~ — ✅ **復旧済み（影綱 4/4 13:35確認）**。XMLタグは既に削除済み。jp_fullmarket_scanner.py（yfinance経由）が正常稼働中（本日朝96銘柄検出）

### 経過観察
4. **position-monitor** — unified-paper-tradeの--monitorモードに統合済み。形式的STALE（実務問題なし）
5. **scalper.py launchd有効化（上様操作要）** — plist作成・syntax check合格済み（3/30）

### 完了済み
- ~~graduation-checker復旧~~ — ✅復旧確認（4/1 14:35）
- ~~graduation_checker ROIバグ修正~~ — ✅完了（3/30）
- ~~卒業軌道シミュレーター~~ — ✅完了（4/1 01:23）

### 現在の損益（4/4 15:49時点）
- 総資産: ¥288,569 (-3.81%) / ポジション: 2件（三菱商事+JT）
- 勝率: 51.4%（144件決済）/ 確定損益: -11,645円 / RR比: 0.62
- ローリングSharpe: -3.20（目標≥0.5 → FAIL） / 最大DD: -6.01%（OK）
- 卒業期限: 4/12（残り8日）/ レバレッジ: 2倍
- vol_div: 唯一黒字（+1,728円、4件・サンプル少）/ bb_rsi: -6,971円（損益の60%）

## 修正履歴（3/30 12:01）
- **graduation_checker ROIバグ修正**: Paper Sharpe |50|超の異常値検出ガード追加。修正前BT乖離5113%→修正後Sharpe差3.11（正常値）。卒業判定が正しく機能するように

## 修正履歴（3/30 07:??）
- **scalper.py launchd plist作成検証完了**: com.danaru.scalper.plist設定確認・syntax check実施。StartInterval=300秒（5分）でスキャルピング対応

## 修正履歴（3/29 06:57）
- **OHLCVデータ取得Noneガード追加**: unified_screener.py, unified_paper_trade.pyの`fetch_data()`にNoneチェック+カラム存在確認を追加。MONA-JPY等のデータ取得失敗時にクラッシュせずスキップするように修正
- **graduation_checker truthy依存修正**: `total_pnl and capital` → `total_pnl is not None and capital`。PnL=0時のROI誤算定を防止
- **crypto_monitor docstring清掃**: Ollama/cron時代の残骸コメントを削除

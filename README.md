# auto-trade — マルチマーケット自動売買システム

株・暗号資産の売買戦略をバックテストし、ウォークフォワード検証で過学習を排除し、
ペーパートレード → 少額実弾へ段階的に移行するためのシステム。

## 現在の状態

- **7つの売買戦略**を実装、全てOptuna最適化済み
- **4市場**で検証済み: 日本株 / 米国株 / BTC / FX
- **ウォークフォワード検証**で過学習チェック → Sony x Monthly Momentumが最高評価（WF Sharpe 1.99）
- **日経225全自動スクリーナー**で有望銘柄を自動発掘
- **ペーパートレード環境**が稼働中（BTC/JPY）
- **実弾投入レイヤー**（bitFlyer ccxt経由）が準備完了（DRY_RUNモード）

---

## 使い方

### 1. シグナル監視（毎朝実行する日課）

```bash
# ウォッチリスト全銘柄のシグナルを一括確認
python3 signal_monitor.py --watchlist

# BUYシグナルの銘柄だけ表示
python3 signal_monitor.py --watchlist --buy-only

# 単一銘柄を確認
python3 signal_monitor.py --symbol 6758.T

# JSON出力（他ツール連携用）
python3 signal_monitor.py --watchlist --json
```

### 2. ペーパートレード（仮想売買で戦略を検証）

```bash
# 日次実行（シグナル判定 → 仮想売買記録）
python3 paper_trade.py

# サマリー確認
python3 paper_trade.py --summary

# 戦略切替
python3 paper_trade.py --strategy bb_rsi

# リセット
python3 paper_trade.py --reset
```

### 3. 仮想通貨自律監視（LLM分析付き）

```bash
# シグナル判定 + ペーパートレード + パフォーマンス記録
python3 crypto_monitor.py

# LLMレポート生成（Ollama qwen2.5:7b使用）
python3 crypto_monitor.py --report

# 卒業条件チェック（実弾投入の準備ができたか確認）
python3 crypto_monitor.py --status

# 全工程実行
python3 crypto_monitor.py --full
```

### 4. 日本株スクリーナー（有望銘柄の自動発掘）

```bash
# 日経225全銘柄をスキャン → WF検証 → watchlist自動更新
python3 jp_stock_screener.py

# Phase 3（WF検証）からのみ実行
python3 jp_stock_screener.py --phase 3

# 上位20銘柄のみWF検証
python3 jp_stock_screener.py --top 20

# watchlist更新なしで結果だけ確認
python3 jp_stock_screener.py --dry-run
```

### 5. 実弾トレード（bitFlyer API経由）

```bash
# ステータス確認（DRY_RUNモードがデフォルト）
python3 live_trade.py --status

# 通常実行（シグナル判定 → DRY_RUN発注シミュレート）
python3 live_trade.py

# 実弾モード（.envにLIVE_TRADE_DRY_RUN=false が必要）
python3 live_trade.py --execute

# 全ポジション強制決済
python3 live_trade.py --close-all

# 戦略切替
python3 live_trade.py --strategy bb_rsi

# リセット
python3 live_trade.py --reset
```

### 6. バックテスト

```bash
# 全7戦略を一括実行
python3 run_backtest.py

# CCXTでBybitからデータ取得
python3 run_backtest.py --source ccxt --symbol BTC/USDT --exchange bybit

# マルチマーケット（日本株/米国株/BTC/FX全て）
python3 run_multi_market.py

# Optunaパラメータ最適化 + ウォークフォワード検証
python3 optimize.py --strategy monthly --symbol 6758.T --walk-forward
```

---

## cron設定例

```cron
# 日本株シグナル監視（毎朝8:50 JST、月〜金）
50 8 * * 1-5 cd /path/to/auto-trade && python3 signal_monitor.py --watchlist >> signal_log.txt 2>&1

# BTC/JPY ペーパートレード（毎日9:00）
0 9 * * * cd /path/to/auto-trade && python3 paper_trade.py >> paper_trade_cron.log 2>&1

# 仮想通貨自律監視（毎時0分）
0 * * * * cd /path/to/auto-trade && python3 crypto_monitor.py >> crypto_monitor.log 2>&1

# LLM日次レポート（毎日9:00）
0 9 * * * cd /path/to/auto-trade && python3 crypto_monitor.py --full >> crypto_monitor.log 2>&1

# 日経225スクリーニング（毎週日曜深夜）
0 0 * * 0 cd /path/to/auto-trade && python3 jp_stock_screener.py >> screening_log.txt 2>&1
```

---

## 実装済みの戦略（7本）

| 戦略 | ファイル | ロジック | 最適市場 |
|------|---------|---------|---------|
| SMAクロスオーバー | `strategies/sma_crossover.py` | 短期MA上抜け→買い、下抜け→売り | — |
| RSI平均回帰 | `strategies/rsi_reversion.py` | RSI30以下→買い、70以上→売り | — |
| BB+RSIコンボ | `strategies/bb_rsi_combo.py` | BB下限+RSI30以下→買い | 米国株 |
| 月初モメンタム | `strategies/monthly_momentum.py` | 月初に出来高急増→買い、月末手仕舞い | **日本株（WF合格）** |
| 出来高ダイバージェンス | `strategies/volume_divergence.py` | MFI+200EMA+VOで出来高と価格の乖離検出 | BTC |
| モメンタムプルバック | `strategies/momentum_pullback.py` | EMA3本+VWAP+MACDで押し目エントリー | — |
| オーダーブロック | `strategies/order_block.py` | 包み足パターンからゾーン特定 | — |

全戦略Optuna最適化済み。パラメータは `optimized_params.json` に保存。

---

## 対応市場

| 市場 | データソース | 銘柄例 | 有望戦略 |
|------|-----------|--------|---------|
| 日本株 | yfinance | 6758.T(Sony), 9984.T(SBG) | **Monthly Momentum（WF合格）** |
| 米国株 | yfinance | AAPL, NVDA, SPY | BB+RSI Combo |
| BTC | yfinance / ccxt (Bybit) | BTC-USD, BTC/USDT | Volume Divergence |
| FX | yfinance | USDJPY=X | 全戦略不適合 |

---

## 卒業条件（ペーパートレード → 実弾投入）

以下の全条件を満たしたら実弾投入を推奨する:

| 条件 | 閾値 |
|------|------|
| ペーパートレード期間 | 最低2週間 |
| 勝率 | 40%以上 |
| ローリングSharpe | 0.5以上 |
| 最大ドローダウン | -15%以内 |
| バックテスト結果との乖離 | +-20%以内 |

`python3 crypto_monitor.py --status` で現在の達成状況を確認できる。

---

## 実弾投入フロー

```
1. バックテスト（run_backtest.py）
   ↓
2. Optuna最適化 + WF検証（optimize.py --walk-forward）
   ↓ WF合格
3. watchlistに登録 → シグナル監視開始（signal_monitor.py）
   ↓
4. ペーパートレード（paper_trade.py / crypto_monitor.py）
   ↓ 卒業条件クリア
5. DRY_RUNテスト（live_trade.py）— 実際の発注はしない
   ↓ 問題なし
6. 少額実弾（live_trade.py --execute）— MAX_ORDER_JPY=10,000
   ↓ 安定稼働
7. 段階的に金額を引き上げ
```

---

## セットアップ

```bash
pip install -r requirements.txt

# 環境変数を設定
cp .env.example .env
# .env をエディタで開いて値を入力
```

### 環境変数（.env）

| 変数名 | 説明 | 必須？ |
|--------|------|--------|
| `ANTHROPIC_API_KEY` | Claude APIキー（cli.py用） | AI生成を使うなら |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL | 監視通知を使うなら |
| `LIVE_TRADE_EXCHANGE` | 取引所（bitflyer） | 実弾トレード時 |
| `LIVE_TRADE_API_KEY` | bitFlyer APIキー | 実弾トレード時 |
| `LIVE_TRADE_API_SECRET` | bitFlyer APIシークレット | 実弾トレード時 |
| `LIVE_TRADE_DRY_RUN` | true=シミュレートのみ | デフォルトtrue |
| `LIVE_TRADE_MAX_ORDER_JPY` | 1回の最大注文額（円） | デフォルト10,000 |
| `LIVE_TRADE_MONTHLY_LOSS_LIMIT_PCT` | 月次損失上限（%） | デフォルト10 |
| `LIVE_TRADE_SYMBOL` | 取引シンボル | デフォルトBTC/JPY |

---

## ファイル構成

```
auto-trade/
├── README.md                      # これ
├── CLAUDE.md                      # プロジェクト状況
├── EXPERIMENTS.md                 # 全検証結果の総合レポート
├── FX_TRADING_RESEARCH.md         # FX自動売買の調査結果
├── requirements.txt               # Pythonパッケージ
├── .env.example                   # 環境変数テンプレート
├── .gitignore
│
│  ===== コアエンジン =====
├── engine.py                      # バックテストエンジン + DataFetcher（YFinance/CCXT）
├── trade_engine.py                # TradeEngine基底クラス（Paper/Live共通インターフェース）
├── optimize.py                    # Optunaパラメータ最適化 + ウォークフォワード検証
│
│  ===== 売買戦略（7本） =====
├── strategies/
│   ├── sma_crossover.py           # SMAクロスオーバー
│   ├── rsi_reversion.py           # RSI平均回帰
│   ├── bb_rsi_combo.py            # BB+RSIコンボ
│   ├── monthly_momentum.py        # 月初モメンタム ★WF合格
│   ├── volume_divergence.py       # 出来高ダイバージェンス
│   ├── momentum_pullback.py       # モメンタムプルバック
│   └── order_block.py             # オーダーブロック
│
│  ===== 監視・売買ツール =====
├── signal_monitor.py              # シグナル監視（単体/ウォッチリスト一括）
├── paper_trade.py                 # ペーパートレード（BTC/JPY仮想売買）
├── live_trade.py                  # 実弾トレード（bitFlyer ccxt経由、DRY_RUNデフォルト）
├── crypto_monitor.py              # 仮想通貨自律監視（Ollama LLM分析付き）
│
│  ===== スクリーナー =====
├── jp_stock_screener.py           # 日経225全自動スクリーニング+WF検証パイプライン
├── screener.py                    # 汎用銘柄スクリーナー（出来高急増+価格上昇）
│
│  ===== バックテスト実行 =====
├── run_backtest.py                # 単一銘柄×全7戦略の一括バックテスト
├── run_multi_market.py            # マルチマーケット（日本株/米国株/BTC/FX）一括
├── run_timeframe_backtest.py      # 短期足（1h/5m）バックテスト
│
│  ===== AI・監視 =====
├── cli.py                         # 対話型CLI（Claude API戦略生成）
├── strategy_agent.py              # AI戦略生成エージェント
├── monitoring_agent.py            # 市場監視 + Discord通知
│
│  ===== 設定・データ =====
├── watchlist.json                 # シグナル監視対象銘柄（WF合格銘柄）
├── optimized_params.json          # Optuna最適化済みパラメータ
├── crypto_config.json             # 仮想通貨監視設定
├── nikkei225_tickers.json         # 日経225銘柄リスト
│
│  ===== 結果・ログ =====
├── walk_forward_results.json      # ウォークフォワード検証結果
├── wf_monthly_momentum_jp.json    # 日本株WF追加検証結果
├── multi_market_results.json      # マルチマーケット検証結果
├── screening_results.json         # スクリーニング結果
├── timeframe_backtest_results.json
├── backtest_results.txt           # バックテスト出力
├── performance_log.json           # パフォーマンス記録
├── paper_positions.json           # ペーパートレード状態
├── paper_trade_log.json           # ペーパートレード取引ログ
├── crypto_daily_report.md         # 仮想通貨日次レポート
├── monitoring_log.txt             # 監視ログ
│
└── plugins/                       # 特徴量エンジン・基底クラス
    ├── indicators/
    │   └── feature_engine.py      # 14指標一括計算（RSI/MACD/BB/ATR/OBV/ADX等）
    └── strategies/
        └── base_strategy.py       # 戦略の基底クラス
```

---

## 技術スタック

| ライブラリ | 用途 |
|-----------|------|
| pandas, numpy | データ処理・数値計算 |
| yfinance | 株価・暗号資産データ取得 |
| ta | テクニカル指標計算（FeatureEngine内部） |
| ccxt | 取引所API（Bybitデータ取得 + bitFlyer発注） |
| optuna | パラメータ自動チューニング |
| scikit-learn | 機械学習（パラメータ評価用） |
| anthropic | Claude API（AI戦略生成） |
| python-dotenv | 環境変数管理 |
| requests | Discord Webhook通知 |

---

## バックテストエンジンの仕組み

- **ルックアヘッドバイアス対策**: シグナルを1日シフトし「翌日始値で執行」を再現
- **手数料**: 0.1%（片道）
- **スリッページ**: 0.05%
- **初期資金**: 100万円
- **ウォークフォワード検証**: 学習12ヶ月 + テスト3ヶ月のローリング

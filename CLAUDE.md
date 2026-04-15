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
- **30万円ペーパートレード稼働中（3/13リセット、4/12期限・残2日）**: 5市場対応。**確定損益-¥10,542・総資産¥289,400（-3.53%）・勝率51.7%（146回）・フラット（0ポジション）**。bb_rsi停止承認待ち27日目。最優秀戦略: vol_div（勝率100%、Sharpe 1.85）。**【上様判断待ち】影綱が「条件付き卒業（vol_div単独）」を推奨（4/10 09:50レポート参照）**
- **戦略別Sharpe比（4/6追加）**: strategy_attribution.py にSharpe比計算追加。vol_div(1.85)/volscale_sma(-0.08)/bb_rsi(-0.23)/monthly(-0.23)。bb_rsiが損失全体67%の主因確定
- **VolScale戦略（動的SMA）稼働中（3/15開始、3/16アルトコインWF検証完了）**: ボラ応じたSMA期間伸縮。**新規エントリーはBTC+ETHのみ**（XRP/XLMはWF不合格→既存ポジション自然決済中）。仮想通貨決済8件(+1,205円)の主力源泉。ETH WF Sharpe 0.52（合格）
- **市場別ファンダメンタル分析（3/13追加）**: 株:PER/PBR, FX:DXY/金利差, BTC:半減期サイクル, Gold:VIX/実質金利
- **ショート+レバレッジ対応済み（3/10追加）**: SELLシグナル+ファンダスコア<0でショートエントリー、レバレッジ2倍
- **ポジション監視15分ごと（3/12追加）**: --monitorモードでSL/TP/強制決済(7日)/早期トレーリングを常時監視
- **自動卒業判定**: graduation_checker.py で毎日9:30に条件チェック
- **統合ダッシュボード**: dashboard.py で全市場の状況を一覧表示
- **戦略別パフォーマンス分析（4/1追加）**: strategy_attribution.py で戦略別集計・市場マトリクス・モメンタム比較・最悪TOP5を自動生成（テスト36件全パス）
- **戦略別アラートシステム（4/6完成）**: strategy_alert.py で4種アラート検出（LOSS_EXCEEDED/SHARPE_LOW/WIN_RATE_DROP/NO_TRADE）。テスト31件全パス。本番稼働中（5件アラート検出）
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
| **strategy_alert.py** | **戦略別アラートシステム（4種検出・Markdown/JSON出力・Discord通知対応）** |
| **pnl_snapshot.py** | **損益スナップショット生成（人間向け/JSON/Markdown）** |
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

## 次の一歩（4/12更新 — 2本柱 GTAA + Turtle 本実装完了）

### 【大転換2 2026-04-12】 攻めの Turtle System 2 も PoC 合格 → 本実装完了
**GTAA だけでは上様の「攻め」の意向を満たせぬため、攻めの R&D を継続。**
**100+ variant 検証を経て 55/20 Turtle System 2 が Crypto + Gold で新3原則突破。**

- Turtle PoC: 175取引・勝率54.3%・WF 4/4合格・+$53,803 (LIVE_TRADE_DESIGN.md §21)
- GTAA + Turtle の2本柱構成で運用する
- 2本柱の期待値: GTAA CAGR 8% + Turtle CAGR 2% + 低相関でSharpe向上
- 月35分の代表負担で回る放置構造

### 2本柱 実装成果物 (Phase 3 完了 4/12)

| 柱 | 戦略 | 実装ファイル | 判定頻度 | launchd |
|---|---|---|---|---|
| **守り** | GTAA (Meb Faber) | `gtaa_live.py` + `gtaa_poc.py` | 月1回 | `com.danaru.gtaa-monthly.plist` ✓ |
| **攻め** | Turtle System 2 (55/20) | `turtle_live.py` + `mtt_4h_trend.py` | 日次 | `com.danaru.turtle-daily.plist` ✓ |

### CRITICAL — Phase 2 上様への依頼 (統合リスト)

Stage 1 開始 (2026-06-01) までに:

1. **米ETF売買口座** — SBI/マネックス/楽天 いずれかで外国株設定を有効化
2. **暗号通貨口座** — 既存の bitFlyer でOK（追加不要）
3. **Discord Webhook URL 更新** — 現在 403 Forbidden。新 URL を `.env` or `~/.zshrc` に設定
4. **launchd 有効化 (4/28頃)**:
   ```bash
   cp /Users/mm16/だーなるAIカンパニー/50_ラボ/auto-trade/com.danaru.gtaa-monthly.plist ~/Library/LaunchAgents/
   cp /Users/mm16/だーなるAIカンパニー/50_ラボ/auto-trade/com.danaru.turtle-daily.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.danaru.gtaa-monthly.plist
   launchctl load ~/Library/LaunchAgents/com.danaru.turtle-daily.plist
   ```
5. **入金 (5月末までに)**: 米ETF ¥130K + bitFlyer ¥70K = 合計 **¥200K** (Stage 1 分)
6. **Stage 0 DRY_RUN 観察** — 5月中は通知と判定ロジックを監視

### Stage 進行計画 (2026年)

| Stage | 期間 | GTAA | Turtle Crypto | Turtle GLD | 合計 |
|---|---|---|---|---|---|
| Stage 0 | 5月 | 仮想10K | 仮想7K | 仮想3K | DRY_RUN |
| **Stage 1** | **6-7月** | **¥100K** | **¥70K** | **¥30K** | **¥200K** |
| Stage 2 | 8-10月 | ¥300K | ¥200K | ¥100K | ¥600K |
| Stage 3 | 11月〜 | ¥1,000K | ¥500K | ¥200K | ¥1,700K |

### 完了済み（4/11-12）
- ✅ 100+ variant 検証 (MTT Attack 4, Variant A, 7 variants runner, 1d sweep, multi-asset)
- ✅ bb_rsi 完全停止
- ✅ GTAA (Meb Faber) 5+13 両方合格
- ✅ Danaru Turtle (55/20 Donchian on Crypto+Gold) 新3原則+WF完全合格
- ✅ gtaa_live.py + turtle_live.py 本実装
- ✅ 両 launchd plist 作成・plutil 検証
- ✅ LIVE_TRADE_DESIGN.md §20-21 本実装章追加
- ✅ 両スクリプトの DRY_RUN 動作確認

### 旧凍結判断
- signal-based 短期戦略 (vol_div/volscale/bb_rsi/monthly/etc.) は **引き続き保留**
- ペーパートレードは **無期限停止** (R&D検証は backtest_live_design.py と gtaa_poc.py と mtt_4h_trend.py が継承)
- `FREEZE_ASSESSMENT.md` は **凍結未執行**
- 旧卒業期限4/12は概念的に不要 (GTAA と Turtle は卒業期限無関係)

### 残留課題（優先度低）
1. position-monitor クラッシュループ — paper停止で優先度低
2. jquants-crawler 全件失敗継続 — 使用予定なし
3. bb_rsi — 既に unified_screener.py から除去済
4. entry_validator LLMパース問題 — 本多智房の edit で改善見込、paper停止で優先度低

### 過去の検証タイムライン (参考)
- **2026-03-04〜04-11**: ペーパートレード5週間 127取引 -3.93%
- **2026-04-11**: 既存9戦略 × BTC × 3時間軸 = 24通り全滅、JP native 12銘柄全滅、Pair Trading 4ペア全滅
- **2026-04-11**: GitHub世界調査・arxiv 2512.12924 発見・GTAA 合格
- **2026-04-11〜12**: MTT 100+ variants 検証・Turtle 55/20 合格
- **2026-04-12**: 両柱本実装完了 (本ドキュメント)

## 修正履歴（4/6 20:06）
- **strategy_alert.py 実装完成**: 4種アラート検出（LOSS_EXCEEDED/SHARPE_LOW/WIN_RATE_DROP/NO_TRADE）。環境変数による動的閾値設定。Discord統合。テスト31件全パス。本番稼働開始（本日5件アラート検出）

## 修正履歴（3/30 12:01）
- **graduation_checker ROIバグ修正**: Paper Sharpe |50|超の異常値検出ガード追加。修正前BT乖離5113%→修正後Sharpe差3.11（正常値）。卒業判定が正しく機能するように

## 修正履歴（3/30 07:??）
- **scalper.py launchd plist作成検証完了**: com.danaru.scalper.plist設定確認・syntax check実施。StartInterval=300秒（5分）でスキャルピング対応

## 修正履歴（3/29 06:57）
- **OHLCVデータ取得Noneガード追加**: unified_screener.py, unified_paper_trade.pyの`fetch_data()`にNoneチェック+カラム存在確認を追加。MONA-JPY等のデータ取得失敗時にクラッシュせずスキップするように修正
- **graduation_checker truthy依存修正**: `total_pnl and capital` → `total_pnl is not None and capital`。PnL=0時のROI誤算定を防止
- **crypto_monitor docstring清掃**: Ollama/cron時代の残骸コメントを削除

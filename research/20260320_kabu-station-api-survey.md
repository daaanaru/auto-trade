# kabu Station API 調査報告

> 調査日: 2026-03-20
> 調査者: 向井 影綱（御庭番）
> 目的: DaNARU auto-tradeシステムとの接続可能性評価

---

## 1. APIの概要

kabu STATION APIは、**auカブコム証券**が提供するRESTful APIサービス。kabu STATION（PC向けトレーディングツール）と連携し、プログラムからの発注・照会・リアルタイム株価取得を可能にする。

### できること

| 機能 | 対応状況 |
|------|---------|
| 現物株の発注・取消・照会 | 対応 |
| 信用取引（制度・一般） | 対応 |
| 先物・オプション取引 | 対応 |
| リアルタイム株価取得（PUSH配信） | 対応（WebSocket） |
| 板情報（気配値）取得 | 対応 |
| 銘柄情報検索 | 対応 |
| 残高照会 | 対応 |
| 注文一覧・約定一覧 | 対応 |
| ランキング情報 | 対応 |

**注意**: 暗号資産・FX・ゴールドには非対応。**日本株（東証）専用**。

---

## 2. 利用条件

| 条件 | 内容 |
|------|------|
| 口座開設 | **auカブコム証券の口座が必須** |
| 費用 | **API利用自体は無料**（取引手数料は通常の売買と同じ） |
| kabu STATION | **PCでkabu STATIONを起動しておく必要あり**（APIはkabu STATION経由で動作） |
| 認証 | APIパスワードでトークン取得（トークン有効期限あり、毎営業日更新推奨） |
| 対象OS | kabu STATIONはWindows専用。**macOS非対応** |

### 重要な制約: macOS非対応

kabu STATIONはWindows専用アプリケーション。Mac mini M4 / MBA M1環境では**直接実行できない**。

回避策:
- Windows VM（Parallels等）上でkabu STATIONを起動し、APIサーバーにアクセス
- VPS（Windows Server）を借りてkabu STATIONを常時起動
- いずれも追加コストが発生する

---

## 3. 対応証券会社

**auカブコム証券（旧カブドットコム証券）のみ**。三菱UFJフィナンシャル・グループ傘下。

- 2019年にKDDI（au）と提携しauカブコム証券に改称
- 手数料体系: ワンショット手数料 or 1日定額手数料（2024年以降の無料化状況は要確認）

---

## 4. 注文API

### 発注（POST /sendorder）

```json
{
  "Password": "注文パスワード",
  "Symbol": "7203",  // トヨタ自動車 ※コンプライアンス除外銘柄は使わない
  "Exchange": 1,
  "SecurityType": 1,
  "Side": "2",
  "CashMargin": 1,
  "DelivType": 2,
  "AccountType": 2,
  "Qty": 100,
  "FrontOrderType": 20,
  "Price": 0
}
```

- 成行・指値・逆指値・IOC等に対応
- 信用取引（新規建て・返済）も同エンドポイント

### 取消（PUT /cancelorder）

```json
{
  "OrderId": "20200529A01N06848002",
  "Password": "注文パスワード"
}
```

### 照会

- `GET /orders` — 注文一覧
- `GET /positions` — 建玉一覧
- `GET /wallet/cash` — 現物余力
- `GET /wallet/margin` — 信用余力

---

## 5. リアルタイム株価取得

### REST（ポーリング）

- `GET /board/{symbol}@{exchange}` — 現値・板情報をREST取得

### WebSocket（PUSH配信）

- `PUT /register` で銘柄登録
- WebSocketで接続するとリアルタイムに価格更新がPUSH配信される
- yfinanceの15分遅延と異なり、**リアルタイム（無遅延）**

---

## 6. Python SDK

### 公式SDK

auカブコム証券がGitHubで公式Pythonライブラリを公開:

- リポジトリ: `kabucom/kabusapi` （GitHub）
- `kabusapi` パッケージ（pip install可能かは要確認）

### サードパーティ

- コミュニティによるラッパーライブラリも複数存在
- REST APIが素直な設計のため、`requests`ライブラリで直接叩くのも容易

### APIの基本フロー（Python例）

```python
import requests

BASE_URL = "http://localhost:18080/kabusapi"  # kabu STATIONのローカルAPI

# 1. トークン取得
token_res = requests.post(f"{BASE_URL}/token", json={
    "APIPassword": "your_api_password"
})
token = token_res.json()["Token"]

headers = {"X-API-KEY": token}

# 2. 板情報取得
board = requests.get(f"{BASE_URL}/board/9433@1", headers=headers)

# 3. 発注
order = requests.post(f"{BASE_URL}/sendorder", headers=headers, json={
    "Password": "order_password",
    "Symbol": "7203",  // トヨタ自動車 ※コンプライアンス除外銘柄は使わない
    "Exchange": 1,
    "SecurityType": 1,
    "Side": "2",
    "CashMargin": 1,
    "DelivType": 2,
    "AccountType": 2,
    "Qty": 100,
    "FrontOrderType": 20,
    "Price": 0
})
```

**ポイント**: APIサーバーは `localhost:18080` で動作（kabu STATIONが起動しているPC上）。

---

## 7. DaNARU auto-tradeシステムとの接続方法

### 現行システムとの比較

| 項目 | yfinance（現行） | kabu Station API |
|------|-----------------|-----------------|
| データ取得 | 15分遅延、無料 | **リアルタイム、無料（口座必要）** |
| 発注機能 | **なし**（データのみ） | **あり（実弾発注可能）** |
| 対応市場 | 日本株/米国株/BTC/FX/Gold | **日本株のみ** |
| 認証 | 不要 | APIパスワード+トークン |
| 依存 | なし | kabu STATION起動必須（Windows） |
| レート制限 | なし（yfinanceの制限あり） | あり（後述） |

### 接続アーキテクチャ案

```
[Mac mini M4]                    [Windows環境]
  auto-trade/                      kabu STATION
  unified_paper_trade.py           (常時起動)
       |                               |
       | HTTP REST                      | localhost:18080
       +---------- VPN/SSH ------------>+
                                   kabu Station API
```

1. **trade_engine.py の新サブクラス `KabuStationEngine` を作成**
   - 既存の `TradeEngine` 基底クラスを継承
   - `execute_buy()` / `execute_sell()` を kabu Station API呼び出しに実装
   - DRY_RUN モードも維持

2. **データ取得の二重化**
   - バックテスト・スクリーニング: yfinance（過去データ豊富）
   - リアルタイム監視・発注: kabu Station API（遅延なし）

3. **Windows環境の用意が最大のハードル**
   - Parallels Desktop: 年額約12,000円
   - Windows VPS: 月額2,000〜5,000円
   - 中古WindowsミニPC: 初期費用のみ

---

## 8. 制約・レート制限

| 制約 | 内容 |
|------|------|
| kabu STATION必須 | PCでkabu STATIONを起動し続ける必要がある |
| Windows専用 | macOSでは直接利用不可 |
| トークン有効期限 | 営業日ごとにトークン再取得が必要 |
| 銘柄登録上限 | WebSocket PUSH配信は50銘柄まで（REST取得は制限なし） |
| 注文回数制限 | 明確な公式数値は非公開だが、短時間の大量発注は制限される可能性あり |
| 取引時間 | 東証の取引時間に準拠（9:00-11:30, 12:30-15:30） |
| 対応市場 | 日本株（東証）のみ。米国株・暗号資産・FXは非対応 |

---

## 9. 総合評価

### メリット（事実）

1. **実弾発注が可能** — yfinanceにはない最大の価値。ペーパートレード卒業後の実弾レイヤーとして使える
2. **リアルタイムデータ** — 15分遅延なし。短期トレード戦略の精度向上
3. **API利用無料** — 口座開設すれば追加費用なし（取引手数料のみ）
4. **REST設計がシンプル** — 既存Pythonコードとの統合が容易

### デメリット（事実）

1. **Windows必須** — Mac環境のDaNARUにとって最大の障壁。追加コスト発生
2. **日本株のみ** — 米国株・BTC・ゴールド・FXには使えない。現行の5市場対応を維持するにはyfinance併用が必須
3. **kabu STATION常時起動** — PCを落とすとAPIが止まる。Mac miniの「放置で回る構造」と矛盾
4. **auカブコム証券専用** — 証券会社が限定される

### 推測（確度表示付き）

- [確度70%] 2024年以降のauカブコム手数料無料化により、APIトレードのコストは低下している可能性がある
- [確度60%] Docker上のWindows環境でkabu STATIONを動かす方法もコミュニティで試みられているが、安定性は不明
- [確度80%] 日本株実弾投入の選択肢としては、SBI証券のHyper SBI 2 APIや楽天証券のマーケットスピードII RSSより、kabu Station APIが最もプログラマブル

---

## 10. DaNARUへの提言

### 今すぐやるべきか？ → **No（時期尚早）**

理由:
- ペーパートレードがまだ卒業基準未達（ローリングSharpe -6.64）
- Windows環境の追加コストに見合う段階ではない
- 日本株のみカバーなので、全市場統合の思想と合わない

### いつ検討すべきか？

- ペーパートレードで日本株戦略（Monthly Momentum）が**単独で卒業基準をクリア**したとき
- 実弾投入の証券口座を選定する段階

### 代替選択肢（次に掘るべき）

1. **SBI証券 API** — 利用者数最大。API提供状況を要調査
2. **楽天証券 マーケットスピードII RSS** — Excel連携型だがPython橋渡し可能か
3. **bitFlyer API（暗号資産）** — 既にccxt経由で接続済み。仮想通貨実弾はこちらが最短

---

## 追加調査項目

**次に掘るべき論点**: SBI証券・楽天証券のAPI提供状況と、日本の主要ネット証券のプログラマティック取引対応の比較表を作成すべき。

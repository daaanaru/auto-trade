# FX・日本株 自動売買 調査レポート

**作成日**: 2026-03-04（日本株セクション追加: 2026-03-04）
**担当**: 御庭番 (Task #29)

---

## 1. OANDA Japan — REST API

### 概要

OANDA証券は日本国内で唯一REST APIを公式に提供するFX業者。

### API仕様

| 項目 | 内容 |
|------|------|
| API種類 | REST API v20 |
| Python SDK | `oandapyV20`（非公式だが事実上標準） |
| ドキュメント | https://developer.oanda.com/rest-live-v20/ |
| サポート | 英語メールのみ（api@oanda.com） |
| 利用料 | 無料（API自体の費用はゼロ） |

### 対応機能

- 成行注文 / 指値注文 / ストップ注文
- トレーリングストップ
- TP（利確）/ SL（損切り）ペア注文
- GTD注文（最大100日期限）
- ポジション照会 / 注文履歴 / 口座状況取得

### 利用条件（最重要）

| 条件 | 内容 |
|------|------|
| コース | **Proコース**が必要 |
| 会員ステータス | **Gold会員以上** |
| Gold会員の条件 | **月間取引額USD50万ドル以上**（約7,500万円） |
| 口座残高 | **25万円以上** |
| APIトークン | 本番口座からのみ発行可能（デモ口座からは不可） |

### デモ口座

| 項目 | 内容 |
|------|------|
| デモ口座開設 | 無料、本番口座なしでも可 |
| 利用期限 | **30日間**（Gold会員なら60日に延長可） |
| API利用 | **Gold会員+Proコース+25万円以上でないとAPIトークン発行不可** |
| environment設定 | `practice`（デモ）/ `live`（本番） |

### 評価

**最大のハードル: Gold会員になるために月7,500万円の取引が必要。**
個人の小規模トレードでは到達不可能。API利用開始のハードルが非常に高い。

### Python実装例

```python
import oandapyV20
from oandapyV20 import API
import oandapyV20.endpoints.orders as orders

api = API(access_token="YOUR_TOKEN", environment="practice")

# 成行注文
data = {
    "order": {
        "type": "MARKET",
        "instrument": "USD_JPY",
        "units": "1000",  # 1000通貨（買い）
        "timeInForce": "FOK",
    }
}
r = orders.OrderCreate(accountID="YOUR_ACCOUNT_ID", data=data)
api.request(r)
```

---

## 2. 国内FX業者のAPI対応状況

### 比較表

| 業者 | REST API | MT4 | MT5 | 自動売買ツール | API自作可 |
|------|----------|-----|-----|-------------|---------|
| **OANDA証券** | あり（条件厳しい） | あり | あり | -- | **可**（Gold会員のみ） |
| **外為ファイネスト** | なし | あり | あり | EA利用可 | MT4/MT5 API経由で可 |
| **FXTF** | なし | あり | **2026年春リリース予定** | EA利用可 | MT4/MT5 API経由で可 |
| **GMOクリック証券** | **終了**（2009年廃止） | なし | なし | -- | **不可（自動売買禁止）** |
| **DMM FX** | なし | なし | なし | -- | 不可 |
| **SBI FXトレード** | なし | なし | なし | 積立FXのみ | 不可 |
| **みんなのFX** | なし | なし | なし | みんなのシストレ | 不可（ツール経由のみ） |
| **ヒロセ通商** | なし | なし | なし | -- | 不可 |
| **GMOコイン FX** | あり（暗号資産FX） | なし | なし | -- | 可（暗号資産FXのみ） |

### 重要な発見

- **GMOクリック証券は自動売買を明確に禁止**している（規約違反でアカウント停止リスク）
- DMM FX、SBI FXトレードにはAPI提供なし
- **実質的にREST APIで自作自動売買が可能なのはOANDA証券のみ**（条件付き）
- MT4/MT5のPython APIを使えば外為ファイネスト、FXTFでも自動売買可能

---

## 3. 手数料・スプレッド比較

### 主要通貨ペアのスプレッド（2026年3月時点）

| 業者 | USD/JPY | EUR/JPY | 取引手数料 |
|------|---------|---------|-----------|
| **OANDA証券**（東京サーバー） | 0.3銭 | 0.5銭 | 無料 |
| **GMOクリック証券** | 0.2銭 | 0.4銭 | 無料 |
| **SBI FXトレード** | 0.18銭（~100万通貨） | 0.38銭 | 無料 |
| **DMM FX** | 0.2銭 | 0.4銭 | 無料 |
| **みんなのFX** | 0.2銭 | 0.4銭 | 無料 |
| **外為ファイネスト** | 0.5銭〜 | 0.7銭〜 | 無料 |

### 分析

- スプレッドはGMOクリック/SBI FX/DMM FXが最安（0.2銭）
- OANDAは0.3銭でやや広い
- **しかしAPI対応はOANDAのみ**
- 外為ファイネストはMT4/MT5対応だがスプレッドが広い
- FX業界は手数料0円が標準。実質コスト=スプレッドのみ

---

## 4. デモ口座でのAPI利用

| 業者 | デモ口座 | デモでAPI利用 | 条件 |
|------|---------|-------------|------|
| **OANDA** | あり（30日） | **不可**（Gold会員のみ） | 月50万ドル取引+25万円入金 |
| **外為ファイネスト** | あり | MT4/MT5デモでEA動作可 | 無条件 |
| **FXTF** | あり | MT4デモでEA動作可 | 無条件 |

### 推奨: MT4/MT5デモ口座ルート

OANDA APIはGold会員のハードルが高すぎるため、**外為ファイネストかFXTFのMT4/MT5デモ口座でEA（自動売買プログラム）を動かす方が現実的**。

MT5 Python APIを使えば、Pythonから直接MT5経由で注文を出せる:

```python
import MetaTrader5 as mt5

mt5.initialize()

# 成行買い
request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": "USDJPY",
    "volume": 0.01,  # 0.01ロット = 1000通貨
    "type": mt5.ORDER_TYPE_BUY,
    "deviation": 20,
}
result = mt5.order_send(request)
```

**注意**: MetaTrader5 PythonパッケージはWindows専用。macOSでは動作しない。Wine経由での動作は不安定。

---

## 5. TradingViewとの連携

### 方法1: TradingView Webhook → Python中継サーバー → OANDA API

```
TradingView（アラート設定）
    ↓ Webhook（HTTP POST）
Python Webサーバー（ngrok等で公開）
    ↓
OANDA REST API（注文実行）
```

**必要なもの:**
- TradingView Proプラン以上（Webhook機能に必要）
- Python中継サーバー（Flask/FastAPI）
- ngrokまたはVPS（Webhook受信用の公開URL）
- OANDA APIトークン（Gold会員必要）

**参考実装**: OANDA公式Lab記事で構築方法が公開済み

### 方法2: TradingView → MT4/MT5（直接連携）

OANDA証券はTradingViewとの口座連携に対応:
- TradingView上から直接OANDA口座で発注可能
- ただし手動発注のみ（自動発注はWebhook経由が必要）

### 方法3: Google Apps Script中継

```
TradingView Webhook → Google Apps Script → OANDA API
```

- サーバー不要（GAS自体がWebhook受信可能）
- 無料で運用可能
- 実装例が複数公開されている

---

## 6. 法的注意点（金融商品取引法）

### 個人がAPI自動売買を行う場合

| 行為 | 合法性 |
|------|--------|
| **自分用にEA/botを作って自分で使う** | **合法**（規制なし） |
| 他人に自動売買ツールを販売する | **要注意**（投資助言・代理業に該当する可能性） |
| 他人のID/パスワードを預かって運用する | **違法**（投資運用業の登録が必要） |
| 特定のFX業者への口座開設を勧誘する | **違法**（第一種金融商品取引業の登録が必要） |

### 重要ポイント

1. **自分で作って自分で使う分には完全に合法**。登録や届出は一切不要
2. 他人に提供・販売する場合は投資助言・代理業の登録が必要になる可能性がある
3. 無登録で投資助言業を行った場合: **5年以下の懲役または500万円以下の罰金**
4. **DaNARUとして自動売買ツールを販売するビジネスは法的リスクが高い**
5. 国内金融庁登録業者（OANDA、外為ファイネスト等）を使えばFX取引自体は合法

---

## 7. 日本株の自動売買API

### 7-1. 三菱UFJ eスマート証券（旧auカブコム証券）— kabuステーション API

**国内証券で唯一、個人投資家向けにREST APIを無償提供。日本株自動売買の最有力候補。**

#### API仕様

| 項目 | 内容 |
|------|------|
| API種類 | REST API + PUSH API（WebSocket） |
| 対応言語 | Python / C# / Java / JavaScript / Excel VBA |
| ドキュメント | https://kabucom.github.io/kabusapi/reference/ |
| GitHub | https://github.com/kabucom/kabusapi |
| 利用料 | **無料** |
| サポート | GitHub コミュニティ（質問・要望） |

#### 対応機能

- **発注**: 現物買い/売り、信用新規/返済、成行/指値/逆指値/IOC
- **市場データ**: リアルタイム株価（PUSH API）、板情報、歩み値
- **照会**: 注文一覧、約定履歴、残高照会、口座情報
- **対応商品**: 株式、先物・オプション、投資信託、FX、CFD
- **テスト環境**: localhost:18081（検証用）/ localhost:18080（本番用）

#### 利用条件

| 条件 | 内容 |
|------|------|
| 口座 | 三菱UFJ eスマート証券の口座（無料開設可） |
| プラン | kabuステーション **Professionalプラン以上** |
| プラン条件 | 信用口座または先物OP口座を開設すれば自動適用 |
| 常時起動 | kabuステーション（Windowsアプリ）を起動しておく必要あり |
| 同時起動 | 1インスタンスのみ |

#### OS対応（最重要）

| OS | 対応状況 |
|----|---------|
| **Windows** | **対応**（kabuステーション本体がWindows専用） |
| **macOS** | **非対応**（ただし回避策あり） |

**macOSでの回避策:**
1. **Parallels Desktop**: Mac上にWindows仮想環境を構築してkabuステーションを起動
2. **Windows VPS**: クラウド上のWindows VPSでkabuステーションを常時起動し、APIをリモートから呼ぶ
3. **nginxリバースプロキシ**: Windows機にnginxを立て、Mac/Linuxからhttp経由でAPI呼び出し

#### 手数料

| 取引 | 手数料 |
|------|--------|
| **信用取引** | **0円（完全無料）** — API経由でも同様 |
| 現物取引 | ワンショットコース: 約定代金による（~100万円で1,089円） |
| 現物（1日定額） | ~100万円で0円 |
| 現物（大口優遇） | **0円** |

**信用取引手数料が完全無料は非常に大きなメリット。**

#### Python実装例

```python
import requests
import json

BASE_URL = "http://localhost:18080/kabusapi"

# トークン取得
token_data = {"APIPassword": "your_password"}
r = requests.post(f"{BASE_URL}/token", json=token_data)
token = r.json()["Token"]

headers = {"X-API-KEY": token}

# 現物買い注文（トヨタ自動車）
order = {
    "Password": "your_trade_password",
    "Symbol": "7203",        # トヨタ
    "Exchange": 1,           # 東証
    "SecurityType": 1,       # 株式
    "Side": "2",            # 買い
    "CashMargin": 1,        # 現物
    "DelivType": 2,         # お預り金
    "AccountType": 2,       # 特定口座
    "Qty": 100,             # 100株
    "FrontOrderType": 10,   # 成行
    "Price": 0,             # 成行なので0
    "ExpireDay": 0,         # 当日限り
}
r = requests.post(f"{BASE_URL}/sendorder", json=order, headers=headers)
print(r.json())
```

#### 評価

**DaNARUにとって最もハードルが低い日本株API。**
- 口座開設 → 信用口座申請 → Professionalプラン自動適用 → API即利用可能
- 信用取引手数料0円は自動売買に最適
- **唯一の問題はWindows専用**（Mac mini M4で直接動かない）
- Windows VPSまたはParallels Desktopが必要

---

### 7-2. 楽天証券 — マーケットスピード II RSS

#### 概要

ExcelのRSS（リアルタイムスプレッドシート）関数を使った自動売買ツール。

| 項目 | 内容 |
|------|------|
| 種類 | Excel アドイン（DDE通信） |
| 利用料 | **無料** |
| 対応市場 | 国内株式（現物・信用）、先物・オプション、商品先物 |
| 自動売買 | VBAマクロ + RSS関数で可能 |
| Python連携 | xlwings / pyautogui 経由で間接的に可能 |

#### 制限

- **Windows専用**（Excel + マーケットスピード II が必要）
- REST APIではない（DDE通信 → Excel → Python の間接連携）
- Pythonから直接発注不可。Excel VBAを中継する必要あり
- 処理速度はkabuステーション APIより遅い

#### 評価

**REST APIではないため、自作プログラムとの親和性が低い。** ExcelベースなのでPython自動売買には不向き。kabuステーション APIの方が圧倒的に優秀。

---

### 7-3. 岡三証券 — 岡三RSS

| 項目 | 内容 |
|------|------|
| 種類 | Excel アドイン |
| 利用料 | **5,093円/35日**（手数料2,000円/35日以上で無料） |
| 初回 | 90日間無料 |
| 対応市場 | 国内株式 |
| Python連携 | Excel経由で間接的に可能 |

#### 評価

**有料（条件付き無料）、Excel専用、Windows専用。** kabuステーション APIの方がすべての面で優れている。

---

### 7-4. SBI証券

| 項目 | 内容 |
|------|------|
| REST API | **個人向けには提供なし** |
| ネオトレAPI for Excel | Excel DDE通信ツール（無料） |
| Python自動売買 | **Selenium（ブラウザ自動操作）で非公式に可能** |

**SBI証券は個人向けREST APIを提供していない。** Seleniumでブラウザ操作する方法はあるが、規約違反リスクあり。非推奨。

---

### 7-5. マネックス証券 — TradeStation（終了済み）

| 項目 | 内容 |
|------|------|
| TradeStation 日本株 | **2020年8月にサービス終了** |
| EasyLanguage | 独自言語でストラテジー記述可能だった |
| 現状 | 利用不可 |

**かつては有力な選択肢だったが、すでにサービス終了。**

---

### 7-6. 日本株API 比較まとめ

| 証券会社 | API種類 | 利用料 | Python直接連携 | macOS | 推奨度 |
|---------|---------|--------|-------------|-------|--------|
| **三菱UFJ eスマート証券** | **REST API** | **無料** | **可** | **非対応** | **最推奨** |
| 楽天証券 | Excel RSS | 無料 | 間接のみ | 非対応 | 非推奨 |
| 岡三証券 | Excel RSS | 有料 | 間接のみ | 非対応 | 非推奨 |
| SBI証券 | なし | -- | Seleniumのみ | -- | 非推奨 |
| マネックス証券 | 終了 | -- | -- | -- | 利用不可 |

**結論: 日本株の自動売買APIは三菱UFJ eスマート証券のkabuステーション API一択。**

---

## 総合評価・推奨

### DaNARUにとっての現実的な選択肢

| 選択肢 | 初期コスト | 難易度 | macOS対応 | 推奨度 |
|--------|-----------|--------|-----------|--------|
| **A: OANDA REST API（FX）** | 25万円+月50万ドル取引 | 中 | 対応 | **非推奨**（Gold会員のハードルが高すぎる） |
| **B: MT4/MT5 + 外為ファイネスト（FX）** | 無料（デモ） | 中 | **非対応** | 条件付き推奨 |
| **C: TradingView Webhook + OANDA（FX）** | TradingView Pro月額 + OANDA Gold | 高 | 対応 | 非推奨（OANDA Gold必要） |
| **D: 仮想通貨（ccxt + bitFlyer/GMO）** | 無料（デモ） | 低〜中 | **対応** | **最推奨** |
| **E: kabuステーション API（日本株）** | 無料（口座開設のみ） | 中 | **非対応** | **推奨**（Windows環境が確保できれば） |

### 最終推奨（優先順位）

**1位: 仮想通貨（ccxt + bitFlyer/GMO）** — 最推奨

- macOS完全対応、デモ環境無料、APIハードルなし
- 既存の `auto-trade/` エンジンがccxt対応済みで、すぐにライブ接続可能
- ボラティリティが高く利益機会が多い

**2位: 日本株（kabuステーション API）** — 推奨（条件付き）

- 国内唯一の個人向け株式REST API。信用取引手数料0円
- **Monthly Momentum戦略がウォークフォワード検証に合格済み**（Sony: WF Sharpe 1.99）
- 課題: Windows環境が必要（VPSまたはParallels Desktop）
- Mac mini M4でParallels + Windows 11を動かせば対応可能

**3位: FX** — 非推奨

- OANDA Gold会員（月7,500万円取引）は個人では到達不可能
- MT4/MT5はWindows専用
- バックテストでもFXは全戦略マイナスで不適合

**もしFXをどうしてもやりたい場合:**
- 外為ファイネストのMT4デモ口座でEAを動かすのが最も現実的
- ただしWindows環境が必要（Parallels Desktop for Macなど）
- あるいはOANDA以外でREST APIを提供する業者が今後出てくるのを待つ

**もし日本株に進む場合の推奨ステップ:**
1. 三菱UFJ eスマート証券の口座開設（無料・オンライン完結）
2. 信用口座の申請（Professionalプラン自動適用）
3. Windows環境の確保（Parallels Desktop or VPS）
4. kabuステーション起動 → APIトークン取得
5. Monthly Momentum戦略をkabuステーション API経由でペーパートレード開始

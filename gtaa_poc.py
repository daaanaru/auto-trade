#!/usr/bin/env python3
"""
GTAA (Meb Faber) PoC — auto-trade 最後の実験

依拠:
  - RND_ALTERNATIVES.md §1 (案A GTAA)
  - Meb Faber "A Quantitative Approach to Tactical Asset Allocation" (2007)
  - LIVE_TRADE_DESIGN.md (凍結判断文脈)

ルール:
  1. 月末の最終営業日に各資産の価格をチェック
  2. 価格 > 10ヶ月SMA なら保有、下なら現金
  3. 保有資産は等ウェイト
  4. 月初（翌月の最初の営業日）にリバランス

新3原則:
  - 最低100判定イベント（= 複数資産 × 月数 ≥ 100）
  - 正期待値（vs Buy&Hold のリスク調整後）
  - Walk-forward 検証（train/test分割で両期間黒字）
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine import YFinanceFetcher


# ==============================================================
# ユニバース（Meb Faber GTAA 5 + 拡張版）
# ==============================================================

GTAA_5 = [
    ("SPY",  "US Large Cap"),
    ("EFA",  "Developed ex-US"),
    ("IEF",  "US 7-10y Treasury"),
    ("GLD",  "Gold"),
    ("VNQ",  "US REITs"),
]

# 拡張版 (GTAA 13 簡易)
GTAA_13 = [
    ("SPY",  "US Large Cap"),
    ("IWM",  "US Small Cap"),
    ("EFA",  "Developed ex-US"),
    ("EEM",  "Emerging Markets"),
    ("IEF",  "US 7-10y Treasury"),
    ("TLT",  "US 20y Treasury"),
    ("GLD",  "Gold"),
    ("DBC",  "Commodities"),
    ("VNQ",  "US REITs"),
]


@dataclass
class GTAAConfig:
    sma_months: int = 10       # Meb Faberの10ヶ月SMA
    rebalance: str = "M"       # 月次
    commission_rate: float = 0.001  # 片道0.1%
    slippage_rate: float = 0.0005
    initial_capital: float = 10000.0


@dataclass
class Position:
    asset: str
    weight: float
    entry_price: float
    entry_date: str


# ==============================================================
# データ取得
# ==============================================================

def fetch_universe(tickers, period="max"):
    """ユニバース全体の月次終値を取得して結合"""
    fetcher = YFinanceFetcher()
    data = {}
    for code, name in tickers:
        try:
            df = fetcher.fetch(code, period=period, interval="1d")
            if df is not None and len(df) > 250:
                # 月末リサンプル
                monthly = df["close"].resample("M").last()
                data[code] = monthly
                print(f"  ✓ {code:5} {name:25}: {len(monthly)}ヶ月分 "
                      f"({monthly.index[0].strftime('%Y-%m')} ～ {monthly.index[-1].strftime('%Y-%m')})")
            else:
                print(f"  ✗ {code:5} {name}: データ不足")
        except Exception as e:
            print(f"  ✗ {code:5} {name}: {e}")
    if not data:
        return None
    # 共通期間にそろえる
    df = pd.DataFrame(data).dropna()
    print(f"\n  共通期間: {df.index[0].strftime('%Y-%m')} ～ {df.index[-1].strftime('%Y-%m')} ({len(df)}ヶ月)")
    return df


# ==============================================================
# GTAA バックテスト
# ==============================================================

def run_gtaa(monthly_close: pd.DataFrame, config: GTAAConfig) -> dict:
    """
    GTAA のイベント駆動バックテスト。
    毎月末に各資産のSMAフィルターを適用し、翌月初リバランス。
    """
    # SMA計算
    sma = monthly_close.rolling(window=config.sma_months).mean()

    n = len(monthly_close)
    assets = list(monthly_close.columns)

    capital = config.initial_capital
    holdings = {}  # asset -> shares

    events = []  # 判定履歴
    equity_curve = []

    for i in range(config.sma_months, n):
        ts = monthly_close.index[i]
        prices = monthly_close.iloc[i]

        # 今月末の時価評価
        mtm_value = capital
        for asset, shares in holdings.items():
            mtm_value += shares * prices[asset] - (shares * holdings_entry[asset] if asset in holdings_entry else 0)

        # 簡易実装: 毎月「現在保有の時価」+ 「現金」を計算する
        # → 以下の方式で書き直す
        break

    # --- 書き直し: より明確な state management ---
    return _run_gtaa_clean(monthly_close, config)


def _run_gtaa_clean(monthly_close: pd.DataFrame, config: GTAAConfig) -> dict:
    """
    明確な state management 版の GTAA BT。

    各月末:
      1. 各資産の「保有条件」(price > 10m SMA) をチェック
      2. 条件を満たす資産のみを等ウェイトで保有
      3. 前月のポジションとの差分を取引としてコスト計上
    """
    sma = monthly_close.rolling(window=config.sma_months).mean()
    n = len(monthly_close)
    assets = list(monthly_close.columns)

    # State: 各資産の保有数量（比率としてのウェイト）
    # 毎月リバランス前に equity を更新
    holdings_value = {a: 0.0 for a in assets}  # 各資産の現在価値
    cash = config.initial_capital

    events = []        # 各月の判定イベント
    equity_curve = []  # 月末の時価評価額
    trade_log = []     # リバランス履歴

    for i in range(n):
        ts = monthly_close.index[i]
        prices = monthly_close.iloc[i]
        sma_row = sma.iloc[i]

        # 今月末の時価評価額 (前月から持ち越した holdings を今月価格で評価)
        # holdings_shares を使う代わりに、前月ウェイト × 前月価格 → 現在のPnLを計算
        # 簡易化: holdings_value は「今月初に配分された」ものとして評価

        # 保有資産の今月末価格での評価
        # 前ループで holdings_value が「月初配分後の金額」として入っている
        # 月末の価値は holdings_value * (prices / prev_prices)
        # ただし毎月リバランスなので、単純に前月末の配分と今月末の変化率で計算する

        if i > 0:
            prev_prices = monthly_close.iloc[i - 1]
            for a in assets:
                if holdings_value[a] > 0 and prev_prices[a] > 0:
                    holdings_value[a] = holdings_value[a] * (prices[a] / prev_prices[a])

        total_equity = cash + sum(holdings_value.values())
        equity_curve.append((ts, total_equity))

        # SMA が計算できるまでスキップ
        if i < config.sma_months - 1 or sma_row.isna().any():
            continue

        # 判定: 条件を満たす資産
        selected = [a for a in assets if prices[a] > sma_row[a]]
        event = {
            "date": ts.strftime("%Y-%m-%d"),
            "selected": selected,
            "n_selected": len(selected),
            "equity": round(total_equity, 2),
        }
        events.append(event)

        # リバランス: 等ウェイトで再配分
        new_holdings = {a: 0.0 for a in assets}
        if selected:
            per_asset_alloc = total_equity / len(selected)
            for a in selected:
                new_holdings[a] = per_asset_alloc
        # selected が空なら全部現金

        new_cash = total_equity - sum(new_holdings.values())

        # 取引コスト計算 (売買した金額に commission + slippage)
        total_trade_value = 0.0
        for a in assets:
            diff = abs(new_holdings[a] - holdings_value[a])
            total_trade_value += diff
        cost = total_trade_value * (config.commission_rate + config.slippage_rate)

        # 反映
        holdings_value = new_holdings
        cash = new_cash - cost

        trade_log.append({
            "date": ts.strftime("%Y-%m-%d"),
            "n_selected": len(selected),
            "selected": selected,
            "trade_value": round(total_trade_value, 2),
            "cost": round(cost, 2),
            "equity_after": round(cash + sum(holdings_value.values()), 2),
        })

    # ベンチマーク: Buy & Hold 等ウェイト（各資産均等配分して保持）
    bh_start_idx = config.sma_months - 1
    bh_start_prices = monthly_close.iloc[bh_start_idx]
    bh_weight = config.initial_capital / len(assets)
    bh_shares = {a: bh_weight / bh_start_prices[a] for a in assets}
    bh_equity_curve = []
    for i in range(bh_start_idx, n):
        prices = monthly_close.iloc[i]
        val = sum(bh_shares[a] * prices[a] for a in assets)
        bh_equity_curve.append((monthly_close.index[i], val))

    # 統計
    gtaa_values = [v for _, v in equity_curve[bh_start_idx:]]
    gtaa_returns = pd.Series(gtaa_values).pct_change().dropna()
    final_equity = gtaa_values[-1]
    total_return = (final_equity / config.initial_capital - 1) * 100

    years = len(gtaa_values) / 12
    cagr = ((final_equity / config.initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Sharpe (月次returns ベース・年率化)
    if gtaa_returns.std() > 0:
        sharpe = (gtaa_returns.mean() / gtaa_returns.std()) * np.sqrt(12)
    else:
        sharpe = 0.0

    # Max DD
    eq_series = pd.Series(gtaa_values)
    dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
    max_dd = dd.min() * 100

    # Buy & Hold 比較
    bh_values = [v for _, v in bh_equity_curve]
    bh_final = bh_values[-1]
    bh_total_return = (bh_final / config.initial_capital - 1) * 100
    bh_cagr = ((bh_final / config.initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    bh_returns = pd.Series(bh_values).pct_change().dropna()
    if bh_returns.std() > 0:
        bh_sharpe = (bh_returns.mean() / bh_returns.std()) * np.sqrt(12)
    else:
        bh_sharpe = 0.0
    bh_eq_series = pd.Series(bh_values)
    bh_dd = (bh_eq_series - bh_eq_series.cummax()) / bh_eq_series.cummax()
    bh_max_dd = bh_dd.min() * 100

    return {
        "events": events,
        "trade_log": trade_log,
        "equity_curve": [(ts.strftime("%Y-%m-%d"), round(v, 2)) for ts, v in equity_curve],
        "gtaa": {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return, 2),
            "cagr_pct": round(cagr, 2),
            "sharpe": round(sharpe, 3),
            "max_dd_pct": round(max_dd, 2),
            "months": len(gtaa_values),
            "years": round(years, 2),
        },
        "buy_hold": {
            "final_equity": round(bh_final, 2),
            "total_return_pct": round(bh_total_return, 2),
            "cagr_pct": round(bh_cagr, 2),
            "sharpe": round(bh_sharpe, 3),
            "max_dd_pct": round(bh_max_dd, 2),
        },
    }


# ==============================================================
# Walk-Forward検証
# ==============================================================

def walk_forward_test(monthly_close: pd.DataFrame, config: GTAAConfig, n_splits: int = 3):
    """データを n_splits 等分し、各分割で独立に検証"""
    total = len(monthly_close)
    split_size = total // n_splits
    results = []
    for k in range(n_splits):
        start = k * split_size
        end = (k + 1) * split_size if k < n_splits - 1 else total
        sub = monthly_close.iloc[start:end]
        if len(sub) < config.sma_months + 12:
            continue
        r = _run_gtaa_clean(sub, config)
        r["period"] = f"{sub.index[0].strftime('%Y-%m')} ～ {sub.index[-1].strftime('%Y-%m')}"
        results.append(r)
    return results


# ==============================================================
# メイン
# ==============================================================

def main():
    print("=" * 90)
    print("  GTAA (Meb Faber) PoC — auto-trade 最後の実験")
    print("  依拠: RND_ALTERNATIVES.md §1, arxiv 2512.12924")
    print("=" * 90)

    config = GTAAConfig()

    # ---- GTAA 5 ----
    print("\n[GTAA 5] データ取得（5資産・最大期間）")
    monthly_5 = fetch_universe(GTAA_5, period="max")
    if monthly_5 is None:
        print("  データ取得失敗")
        return

    print("\n[GTAA 5] バックテスト実行")
    result_5 = _run_gtaa_clean(monthly_5, config)
    print_result("GTAA 5", result_5)

    print("\n[GTAA 5] Walk-Forward検証 (3分割)")
    wf_5 = walk_forward_test(monthly_5, config, n_splits=3)
    print_wf("GTAA 5", wf_5)

    # ---- GTAA 13 ----
    print("\n\n[GTAA 13] データ取得（9資産・最大期間）")
    monthly_13 = fetch_universe(GTAA_13, period="max")

    result_13 = None
    wf_13 = []
    if monthly_13 is not None and len(monthly_13) >= 24:
        print("\n[GTAA 13] バックテスト実行")
        result_13 = _run_gtaa_clean(monthly_13, config)
        print_result("GTAA 13", result_13)

        print("\n[GTAA 13] Walk-Forward検証 (3分割)")
        wf_13 = walk_forward_test(monthly_13, config, n_splits=3)
        print_wf("GTAA 13", wf_13)
    else:
        print("  データ期間不足（EEM, DBC 等が1990年代後半から始まるため）")

    # ---- 新3原則判定 ----
    print("\n\n" + "=" * 90)
    print("  新3原則判定")
    print("=" * 90)

    def evaluate(name, result, wf):
        print(f"\n  === {name} ===")
        if result is None:
            print("    → 評価不能（データ不足）")
            return
        n_events = len(result["events"])
        gtaa = result["gtaa"]
        bh = result["buy_hold"]

        # 原則①: 最低100判定イベント
        ok_samples = n_events >= 100
        print(f"  原則①: 100判定イベント      → {'✓' if ok_samples else '✗'} ({n_events}イベント)")

        # 原則②: 正期待値(総合) & vs buy-hold のリスク調整後
        ok_positive = gtaa["total_return_pct"] > 0
        # リスク調整: Sharpe比較。GTAA Sharpe > BH Sharpe
        ok_risk_adj = gtaa["sharpe"] > bh["sharpe"]
        ok_dd_better = gtaa["max_dd_pct"] > bh["max_dd_pct"]  # 負数なので大きい方が浅い
        print(f"  原則②-a: 正リターン          → {'✓' if ok_positive else '✗'} ({gtaa['total_return_pct']:+.2f}%)")
        print(f"  原則②-b: vs B&H Sharpe改善   → {'✓' if ok_risk_adj else '✗'} (GTAA {gtaa['sharpe']:.2f} vs BH {bh['sharpe']:.2f})")
        print(f"  原則②-c: vs B&H DD浅い       → {'✓' if ok_dd_better else '✗'} (GTAA {gtaa['max_dd_pct']:.1f}% vs BH {bh['max_dd_pct']:.1f}%)")

        # 原則③: WF全分割で正リターン
        ok_wf = all(r["gtaa"]["total_return_pct"] > 0 for r in wf) if wf else False
        if wf:
            wf_returns = [r["gtaa"]["total_return_pct"] for r in wf]
            print(f"  原則③: WF全分割で正リターン → {'✓' if ok_wf else '✗'} ({wf_returns})")
        else:
            print(f"  原則③: WF全分割で正リターン → ✗ (分割失敗)")

        all_pass = ok_samples and ok_positive and ok_risk_adj and ok_wf
        print(f"\n  → 総合判定: {'◎ 合格' if all_pass else '✗ 不合格'}")
        return all_pass

    pass_5 = evaluate("GTAA 5", result_5, wf_5)
    pass_13 = evaluate("GTAA 13", result_13, wf_13)

    # 出力
    output_file = PROJECT_ROOT / "gtaa_poc_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "config": {
                "sma_months": config.sma_months,
                "commission_rate": config.commission_rate,
                "slippage_rate": config.slippage_rate,
                "initial_capital": config.initial_capital,
            },
            "gtaa_5": {
                "universe": [{"code": c, "name": n} for c, n in GTAA_5],
                "result": result_5,
                "walk_forward": [
                    {"period": r["period"], "gtaa": r["gtaa"], "buy_hold": r["buy_hold"]}
                    for r in wf_5
                ],
                "verdict": "PASS" if pass_5 else "FAIL",
            },
            "gtaa_13": {
                "universe": [{"code": c, "name": n} for c, n in GTAA_13],
                "result": result_13,
                "walk_forward": [
                    {"period": r["period"], "gtaa": r["gtaa"], "buy_hold": r["buy_hold"]}
                    for r in wf_13
                ] if wf_13 else [],
                "verdict": "PASS" if pass_13 else "FAIL",
            },
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n📁 保存: {output_file}")


def print_result(name, r):
    g = r["gtaa"]
    bh = r["buy_hold"]
    print(f"\n  【{name} 結果】期間 {g['years']}年 ({g['months']}ヶ月)")
    print(f"  GTAA     : 資本¥{g['final_equity']:,.0f} Return {g['total_return_pct']:+.2f}% "
          f"CAGR {g['cagr_pct']:+.2f}% Sharpe {g['sharpe']:.2f} DD {g['max_dd_pct']:.1f}%")
    print(f"  Buy&Hold : 資本¥{bh['final_equity']:,.0f} Return {bh['total_return_pct']:+.2f}% "
          f"CAGR {bh['cagr_pct']:+.2f}% Sharpe {bh['sharpe']:.2f} DD {bh['max_dd_pct']:.1f}%")


def print_wf(name, wf_results):
    if not wf_results:
        print(f"  {name}: WF結果なし")
        return
    print(f"  {name} WF ({len(wf_results)}分割):")
    for i, r in enumerate(wf_results, 1):
        g = r["gtaa"]
        bh = r["buy_hold"]
        print(f"    [Fold {i}] {r['period']}: "
              f"GTAA Return {g['total_return_pct']:+.2f}% (Sharpe {g['sharpe']:.2f}) "
              f"| BH {bh['total_return_pct']:+.2f}% (Sharpe {bh['sharpe']:.2f})")


if __name__ == "__main__":
    main()

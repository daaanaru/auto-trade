#!/usr/bin/env python3
"""
strategy_explorer.py — ML戦略収斂バイアス対策ツール

問題: Claude Codeで戦略探索すると、同一コンテキスト内で過去の成功例に
引っ張られ、新しい戦略が正当に評価されなくなる（収斂バイアス）。

解決: 各戦略探索を独立プロセス（claude -p）で実行し、コンテキストを分離する。
各探索エージェントは他の戦略の結果を知らない状態で独立に評価する。

使い方:
    # 市場と探索テーマを指定して並列探索
    python3 strategy_explorer.py --market jp_stock --themes 3

    # 特定の探索指示を与えて実行
    python3 strategy_explorer.py --market crypto --prompt "ボラティリティブレイクアウト戦略"

    # 結果の比較レポートを生成
    python3 strategy_explorer.py --compare
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
RESULTS_DIR = PROJECT_ROOT / "exploration_results"
RESULTS_DIR.mkdir(exist_ok=True)

# 探索テーマのテンプレート（市場別）
EXPLORATION_THEMES = {
    "jp_stock": [
        "日本株の月初アノマリーを利用したモメンタム戦略。月初5営業日の値動きが月間リターンを予測するか検証",
        "日本株のセクターローテーション戦略。相対強度(RS)で業種間の資金移動を検知し、強いセクターに乗る",
        "日本株の決算発表前後のボラティリティを利用した戦略。決算日の2日前にエントリーし、発表翌日に決済",
        "日本株の出来高急増+価格ブレイクアウト戦略。過去20日の出来高平均の3倍以上かつ高値更新でエントリー",
        "日本株の移動平均乖離率回帰戦略。25日線から-5%以上乖離で買い、+5%以上で売り",
    ],
    "us_stock": [
        "米国株のオーバーナイトリターン戦略。引け値で買い翌日寄付で売る。ギャップアップの統計的優位性を検証",
        "S&P500構成銘柄のミーンリバージョン戦略。RSI(2)が10以下で買い、90以上で売り",
        "米国株のVIX連動戦略。VIX急上昇（前日比+20%以上）翌日にSPYロング。恐怖のピークで逆張り",
        "米国テック株のモメンタム+ファンダメンタル合成戦略。12ヶ月リターン上位+PEG ratio 1以下でスクリーニング",
        "米国株の配当落ち日戦略。高配当株を配当落ち3日前に買い、落ち日当日に売り",
    ],
    "crypto": [
        "BTCのファンディングレート逆張り戦略。永久先物のファンディングレートが極端に正/負のときにカウンタートレード",
        "BTC/ETHのスプレッド戦略。BTC/ETH比率が過去30日の2σ帯を超えたらリバーサル",
        "仮想通貨の取引所間スプレッド戦略。Binance/Bybit間の価格差が0.3%以上で裁定",
        "BTCのオンチェーンデータ戦略。MVRV比率が1以下で買い、3以上で売り",
        "仮想通貨の時間帯アノマリー戦略。UTC 0-4時（アジア市場）の値動きが他の時間帯と異なるか検証",
    ],
    "gold": [
        "ゴールドのインフレヘッジ戦略。実質金利（10年国債利回り-CPI）が負のときにロング",
        "ゴールドの地政学リスク戦略。VIX>30かつドル安（DXY下落）のときにロング",
        "ゴールドの季節性戦略。インドの結婚シーズン（10-12月）前の需要増を狙う",
    ],
}

# 各探索エージェントに渡すプロンプトテンプレート
EXPLORER_PROMPT_TEMPLATE = """あなたは独立した戦略探索エージェントです。
以下の戦略アイデアを、バックテストで検証してください。

## 重要な制約
- 他の戦略の結果は一切参照しないでください
- この戦略単体の長所・短所を客観的に評価してください
- 過去に成功した戦略パターンに引っ張られないでください

## 探索指示
市場: {market}
テーマ: {theme}

## 手順
1. テーマを具体的な売買ルールに分解する
2. {project_root}/engine.py を使ってバックテストを実行する
   - 対象期間: 過去2年間
   - 初期資金: 1,000,000 JPY
3. 結果を以下のJSON形式で出力する

## 出力形式（必ずこの形式で出力すること）
```json
{{
  "theme": "テーマ名",
  "market": "{market}",
  "strategy_description": "戦略の具体的な売買ルール",
  "parameters": {{}},
  "backtest_result": {{
    "total_return_pct": 0.0,
    "sharpe_ratio": 0.0,
    "max_drawdown_pct": 0.0,
    "win_rate_pct": 0.0,
    "total_trades": 0,
    "avg_holding_days": 0
  }},
  "strengths": ["強み1", "強み2"],
  "weaknesses": ["弱み1", "弱み2"],
  "verdict": "PROMISING / NEUTRAL / REJECT",
  "verdict_reason": "判定理由"
}}
```

バックテストが実行できない場合（データ不足、指標が未実装など）は、
verdict=REJECTとし、理由をverdict_reasonに書いてください。

結果のJSONだけを出力してください。説明文は不要です。
"""


def run_exploration(market: str, theme: str, idx: int) -> dict:
    """1つの戦略テーマを独立プロセスで探索する"""
    prompt = EXPLORER_PROMPT_TEMPLATE.format(
        market=market,
        theme=theme,
        project_root=str(PROJECT_ROOT),
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = RESULTS_DIR / f"explore_{market}_{idx}_{timestamp}.json"

    print(f"  [{idx}] 探索開始: {theme[:50]}...")

    try:
        # claude -p で独立コンテキストで実行
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=300,  # 5分タイムアウト
            cwd=str(PROJECT_ROOT),
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )

        output = proc.stdout.strip()

        # JSONを抽出
        json_match = None
        # ```json ... ``` ブロックを探す
        import re
        json_block = re.search(r'```json\s*\n(.*?)\n```', output, re.DOTALL)
        if json_block:
            json_match = json_block.group(1)
        else:
            # 生のJSONを探す
            for line_start in range(len(output)):
                if output[line_start] == '{':
                    try:
                        json_match = json.loads(output[line_start:])
                        json_match = output[line_start:]
                        break
                    except json.JSONDecodeError:
                        continue

        if json_match:
            result = json.loads(json_match) if isinstance(json_match, str) else json_match
            result["exploration_id"] = f"{market}_{idx}_{timestamp}"
            result["raw_output_length"] = len(output)

            result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"  [{idx}] 完了: {result.get('verdict', '?')} (Sharpe: {result.get('backtest_result', {}).get('sharpe_ratio', '?')})")
            return result
        else:
            error_result = {
                "theme": theme,
                "market": market,
                "verdict": "ERROR",
                "verdict_reason": f"JSON抽出失敗。出力長: {len(output)}文字",
                "raw_output": output[:500],
            }
            result_file.write_text(json.dumps(error_result, ensure_ascii=False, indent=2))
            print(f"  [{idx}] エラー: JSON抽出失敗")
            return error_result

    except subprocess.TimeoutExpired:
        error_result = {
            "theme": theme,
            "market": market,
            "verdict": "ERROR",
            "verdict_reason": "タイムアウト（5分）",
        }
        result_file.write_text(json.dumps(error_result, ensure_ascii=False, indent=2))
        print(f"  [{idx}] タイムアウト")
        return error_result
    except FileNotFoundError:
        print(f"  [{idx}] エラー: claude コマンドが見つかりません")
        return {"verdict": "ERROR", "verdict_reason": "claude CLI not found"}
    except Exception as e:
        print(f"  [{idx}] エラー: {e}")
        return {"verdict": "ERROR", "verdict_reason": str(e)}


def run_parallel_exploration(market: str, num_themes: int = 3):
    """複数テーマを順次実行（claude CLIは並列実行非推奨のため）"""
    themes = EXPLORATION_THEMES.get(market, EXPLORATION_THEMES["jp_stock"])[:num_themes]

    print(f"\n{'='*60}")
    print(f"戦略探索（コンテキスト分離モード）")
    print(f"市場: {market} / テーマ数: {len(themes)}")
    print(f"{'='*60}\n")

    results = []
    for i, theme in enumerate(themes):
        result = run_exploration(market, theme, i)
        results.append(result)
        # 連続実行の間に少し待つ（API負荷軽減）
        if i < len(themes) - 1:
            time.sleep(2)

    return results


def compare_results():
    """exploration_results/ 内の全結果を比較してレポート生成"""
    result_files = sorted(RESULTS_DIR.glob("explore_*.json"))
    if not result_files:
        print("結果ファイルがありません。先に探索を実行してください。")
        return

    results = []
    for f in result_files:
        try:
            results.append(json.loads(f.read_text()))
        except Exception:
            continue

    print(f"\n{'='*60}")
    print(f"戦略探索結果比較レポート（{len(results)}件）")
    print(f"{'='*60}\n")

    # verdictでソート（PROMISING > NEUTRAL > REJECT > ERROR）
    verdict_order = {"PROMISING": 0, "NEUTRAL": 1, "REJECT": 2, "ERROR": 3}
    results.sort(key=lambda r: (
        verdict_order.get(r.get("verdict", "ERROR"), 9),
        -(r.get("backtest_result", {}).get("sharpe_ratio", -999))
    ))

    for r in results:
        verdict = r.get("verdict", "?")
        market = r.get("market", "?")
        theme = r.get("theme", "?")[:50]
        bt = r.get("backtest_result", {})
        sharpe = bt.get("sharpe_ratio", "N/A")
        dd = bt.get("max_drawdown_pct", "N/A")
        wr = bt.get("win_rate_pct", "N/A")

        icon = {"PROMISING": "🟢", "NEUTRAL": "🟡", "REJECT": "🔴", "ERROR": "⚫"}.get(verdict, "?")
        print(f"{icon} [{verdict:10s}] {market:10s} | Sharpe: {sharpe:>8s} | DD: {dd:>8s} | WR: {wr:>8s}")
        print(f"   {theme}")
        if r.get("verdict_reason"):
            print(f"   理由: {r['verdict_reason'][:80]}")
        print()

    # PROMISING結果をサマリー
    promising = [r for r in results if r.get("verdict") == "PROMISING"]
    if promising:
        print(f"\n🟢 有望戦略: {len(promising)}件")
        for r in promising:
            print(f"  - {r.get('theme', '?')}")
            print(f"    {r.get('strategy_description', '')[:100]}")
    else:
        print("\n有望戦略はまだありません。探索テーマを増やすか、別の市場を試してください。")

    # レポートファイルに保存
    report_path = RESULTS_DIR / f"comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n比較レポート保存: {report_path}")


def main():
    parser = argparse.ArgumentParser(description="ML戦略探索（コンテキスト分離モード）")
    parser.add_argument("--market", choices=["jp_stock", "us_stock", "crypto", "gold"],
                        default="jp_stock", help="対象市場")
    parser.add_argument("--themes", type=int, default=3, help="探索テーマ数")
    parser.add_argument("--prompt", type=str, help="カスタム探索指示")
    parser.add_argument("--compare", action="store_true", help="結果比較レポートを生成")

    args = parser.parse_args()

    if args.compare:
        compare_results()
        return

    if args.prompt:
        # カスタムプロンプトで単一探索
        result = run_exploration(args.market, args.prompt, 0)
        print(f"\n結果: {json.dumps(result, ensure_ascii=False, indent=2)}")
    else:
        # テンプレートから並列探索
        results = run_parallel_exploration(args.market, args.themes)
        print(f"\n{'='*60}")
        print(f"探索完了: {len(results)}件")
        promising = sum(1 for r in results if r.get("verdict") == "PROMISING")
        print(f"有望: {promising}件")
        print(f"結果は {RESULTS_DIR}/ に保存済み")
        print(f"比較: python3 strategy_explorer.py --compare")


if __name__ == "__main__":
    main()

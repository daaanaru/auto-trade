"""
TradingPlatform CLI
「この手法試して」と入力するだけでAIが戦略を生成・バックテストする

使い方:
    python cli.py
    python cli.py --prompt "RSIとBBを組み合わせた逆張り戦略をBTCで試して"
"""

import argparse
import json
import os
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.strategy_agent import StrategyAgent


BANNER = """
╔══════════════════════════════════════════════════════════╗
║          🤖 TradingPlatform - AI Strategy Engine         ║
║    自然言語で指示するだけで自動売買戦略が完成します      ║
╚══════════════════════════════════════════════════════════╝
"""

HELP_TEXT = """
コマンド一覧:
  generate / g  : 新しい戦略を生成
  list / l      : 登録済み戦略の一覧
  code <name>   : 戦略のコードを表示
  help / h      : このヘルプを表示
  exit / q      : 終了

使用例:
  > generate
  > RSIが30以下でBBバンド下抜けしたら買う戦略をBTCで試して

  > generate
  > Xでみたやつ。米国株で毎月最初の3日間に出来高上位20銘柄を買って月末に売る

  > list
"""


def print_strategy_list(agent: StrategyAgent):
    """登録済み戦略の一覧を表示"""
    strategies = agent.list_strategies()
    if not strategies:
        print("  まだ戦略が登録されていません。")
        return
    
    print(f"\n  登録済み戦略: {len(strategies)}件")
    print("  " + "-" * 60)
    for i, s in enumerate(strategies, 1):
        status_icon = {"draft": "📝", "paper": "🧪", "live": "🟢", "retired": "⛔"}.get(s.get("status", ""), "❓")
        print(f"  {i:2d}. {status_icon} {s['name']}")
        print(f"      市場: {s['market']}  |  作成: {s['created_at'][:10]}")
        print(f"      元指示: {s.get('origin_prompt', '')[:50]}...")
        print()


def interactive_loop(agent: StrategyAgent):
    """対話型ループ"""
    print(BANNER)
    print(HELP_TEXT)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 終了します。")
            break

        if not user_input:
            continue

        cmd = user_input.lower().split()[0]

        # 終了
        if cmd in ("exit", "quit", "q"):
            print("👋 終了します。")
            break

        # ヘルプ
        elif cmd in ("help", "h"):
            print(HELP_TEXT)

        # 戦略一覧
        elif cmd in ("list", "l"):
            print_strategy_list(agent)

        # コード表示
        elif cmd == "code":
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("使い方: code <戦略名>")
                continue
            code = agent.get_strategy_code(parts[1])
            if code:
                print(f"\n```python\n{code}\n```")
            else:
                print(f"  戦略 '{parts[1]}' が見つかりません")

        # 戦略生成
        elif cmd in ("generate", "gen", "g"):
            print("どんな手法ですか？（自由に説明してください）")
            print("例: 「RSIが30以下になったら買い、70以上で売る。BTCで試したい」")
            try:
                prompt = input("  戦略> ").strip()
            except (KeyboardInterrupt, EOFError):
                continue
            if prompt:
                result = agent.generate(prompt)
                if result["success"]:
                    print("\n✅ 戦略が生成されました！")
                    print(f"   保存先: {result['file_path']}")
                    _ask_next_action(agent, result)
                else:
                    print(f"\n❌ 生成失敗: {result['error']}")
            else:
                print("  キャンセルしました")

        # 直接プロンプトとして処理（コマンドでない場合）
        else:
            print(f"\n🤖 「{user_input[:50]}...」を戦略として解釈します\n")
            result = agent.generate(user_input)
            if result["success"]:
                print("\n✅ 戦略が生成されました！")
                _ask_next_action(agent, result)
            else:
                print(f"\n❌ 生成失敗: {result['error']}")
                print("ヒント: 'generate' コマンドで対話形式で入力できます")


def _ask_next_action(agent: StrategyAgent, result: dict):
    """生成後の次のアクションを確認"""
    print("\n次のアクション:")
    print("  [1] バックテストを実行（実装予定）")
    print("  [2] 戦略を改善する")
    print("  [3] 戦略コードを表示")
    print("  [Enter] スキップ")

    try:
        choice = input("  選択> ").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if choice == "2":
        print("改善要望を入力してください:")
        try:
            feedback = input("  改善> ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if feedback:
            improved = agent.improve(
                original_code=result["code"],
                backtest_result={},
                feedback=feedback,
            )
            if improved["success"]:
                print("\n✅ 改善案が生成されました！")
                print("  バックテストで検証してから保存するか確認してください")

    elif choice == "3":
        print(f"\n```python\n{result['code']}\n```")


def main():
    parser = argparse.ArgumentParser(description="TradingPlatform CLI")
    parser.add_argument("--prompt", "-p", type=str, help="戦略生成プロンプト（非対話モード）")
    parser.add_argument("--api-key", type=str, help="Anthropic API Key")
    parser.add_argument("--model", type=str, default="claude-opus-4-6", help="使用するモデル")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEYが設定されていません")
        print("   export ANTHROPIC_API_KEY='your-key' で設定してください")
        sys.exit(1)

    agent = StrategyAgent(api_key=api_key, model=args.model)

    if args.prompt:
        # 非対話モード
        result = agent.generate(args.prompt)
        if result["success"]:
            print(f"\n✅ 完了: {result['file_path']}")
        else:
            print(f"\n❌ 失敗: {result['error']}")
            sys.exit(1)
    else:
        # 対話モード
        interactive_loop(agent)


if __name__ == "__main__":
    main()

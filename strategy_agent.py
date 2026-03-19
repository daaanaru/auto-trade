"""
StrategyAgent: 自然言語の指示をトレード戦略コードに変換するAIエージェント

使い方:
    agent = StrategyAgent(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    result = await agent.run("RSIが30以下でBB下抜けしたら買う戦略をBTCで試して")
"""

import anthropic
import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルート = auto-trade/ 自身（生成物をリポ内に保持する）
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# システムプロンプト（AIへの指示書）
# ============================================================

STRATEGY_SYSTEM_PROMPT = """
あなたは「TradingPlatform」の戦略生成AIです。
ユーザーの自然言語の指示から、Pythonのトレーディング戦略クラスを生成します。

## あなたの役割
1. ユーザーの指示を解釈して戦略ロジックを設計する
2. BaseStrategyを継承したPythonクラスを生成する
3. 設計の意図・パラメータ・リスクを説明する

## 出力フォーマット（必ずこの形式で返すこと）

<analysis>
ユーザー指示の解釈と戦略設計の説明（日本語）
- 何を検知して売買するか
- 使用する指標とパラメータ
- 想定リスクと対策
</analysis>

<strategy_code>
```python
# 必ずBaseStrategyを継承すること
# 必ずgenerate_signals()とposition_size()を実装すること
# pandasとnumpyは使用可能

import numpy as np
import pandas as pd
from plugins.strategies.base_strategy import BaseStrategy, StrategyMeta

class GeneratedStrategy(BaseStrategy):
    \"\"\"戦略の説明\"\"\"
    
    def __init__(self):
        meta = StrategyMeta(
            name="戦略名（英数字とアンダースコアのみ）",
            market="us_stock | jp_stock | crypto | fx のいずれか",
            origin_prompt="ユーザーの指示文をそのまま記録",
            description="戦略の説明",
            tags=["タグ1", "タグ2"],
        )
        params = {
            # パラメータ定義（最適化対象）
        }
        super().__init__(meta=meta, params=params)
    
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        \"\"\"
        data列: open, high, low, close, volume（全て小文字）
        Returns: pd.Series（1:買, -1:売, 0:待機）
        \"\"\"
        signals = pd.Series(0, index=data.index)
        # ... ロジック実装 ...
        return signals
    
    def position_size(self, signal: int, portfolio_value: float, price: float) -> float:
        \"\"\"ポジションサイズ（株数・枚数）\"\"\"
        risk_pct = self.params.get("risk_per_trade", 0.02)  # 2%リスク
        return (portfolio_value * risk_pct) / price
```
</strategy_code>

<params_explanation>
各パラメータの説明と推奨チューニング範囲
</params_explanation>

<risks>
この戦略の主なリスクと注意点
</risks>
"""

IMPROVE_SYSTEM_PROMPT = """
あなたはトレーディング戦略の改善AIです。
バックテスト結果を受け取り、戦略コードを改善します。

ユーザーの改善要望と現在のバックテスト結果に基づいて、
既存の戦略コードを修正・改善してください。

出力フォーマットはSYSTEM_PROMPTと同じ形式を使ってください。
改善点を<analysis>に明記すること。
"""


# ============================================================
# メインエージェントクラス
# ============================================================

class StrategyAgent:
    """
    自然言語 → 戦略コード生成エージェント
    
    主なメソッド:
        generate(prompt)   : 新戦略を生成
        improve(code, result, feedback): 既存戦略を改善
        validate_code(code): 生成コードの安全性チェック
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-opus-4-6"):
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.model = model
        self.strategies_dir = PROJECT_ROOT / "plugins" / "strategies"
        self.registry_path = PROJECT_ROOT / "registry" / "strategies.json"
        self._ensure_registry()

    # ----------------------------------------------------------
    # 新戦略生成
    # ----------------------------------------------------------

    def generate(self, user_prompt: str, verbose: bool = True) -> dict:
        """
        自然言語の指示から戦略を生成する。
        
        Returns:
            {
                "success": bool,
                "analysis": str,        # AIの解釈・設計説明
                "code": str,            # 生成されたPythonコード
                "params_explanation": str,
                "risks": str,
                "strategy_class": class | None,  # 検証済みクラス
                "file_path": str | None, # 保存先
                "error": str | None,
            }
        """
        if verbose:
            print(f"\n🤖 Agent > 指示を解釈中...")
            print(f"   📝 \"{user_prompt[:60]}{'...' if len(user_prompt) > 60 else ''}\"\n")

        # Claude APIを呼び出して戦略を生成
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=STRATEGY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}]
            )
            raw_output = response.content[0].text
        except Exception as e:
            return {"success": False, "error": f"API呼び出しエラー: {e}"}

        # レスポンスをパース
        parsed = self._parse_response(raw_output)
        if not parsed["success"]:
            return parsed

        if verbose:
            print("✅ 戦略コード生成完了\n")
            print("=" * 50)
            print(parsed["analysis"])
            print("=" * 50)

        # コードの安全性チェック＆クラスロード
        validation = self._validate_and_load(parsed["code"])
        if not validation["success"]:
            if verbose:
                print(f"❌ コード検証失敗: {validation['error']}")
            return {**parsed, **validation}

        strategy_class = validation["strategy_class"]
        strategy_instance = strategy_class()

        # ファイルに保存
        file_path = self._save_strategy(
            code=parsed["code"],
            strategy=strategy_instance,
            origin_prompt=user_prompt
        )

        # レジストリに登録
        self._register_strategy(strategy_instance, file_path, user_prompt)

        if verbose:
            print(f"✅ 戦略を保存: {file_path}")
            print(f"\n⚠️  リスク:\n{parsed['risks']}\n")

        return {
            **parsed,
            "success": True,
            "strategy_class": strategy_class,
            "strategy_instance": strategy_instance,
            "file_path": str(file_path),
            "error": None,
        }

    # ----------------------------------------------------------
    # 既存戦略の改善
    # ----------------------------------------------------------

    def improve(
        self,
        original_code: str,
        backtest_result: dict,
        feedback: str,
        verbose: bool = True
    ) -> dict:
        """
        バックテスト結果とフィードバックを元に戦略を改善する。
        """
        if verbose:
            print(f"\n🔧 Agent > 戦略を改善中...")
            print(f"   要望: \"{feedback}\"\n")

        improvement_prompt = f"""
以下の戦略を改善してください。

## 改善要望
{feedback}

## 現在のバックテスト結果
{json.dumps(backtest_result, ensure_ascii=False, indent=2)}

## 現在のコード
```python
{original_code}
```
"""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=IMPROVE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": improvement_prompt}]
            )
            raw_output = response.content[0].text
        except Exception as e:
            return {"success": False, "error": f"API呼び出しエラー: {e}"}

        parsed = self._parse_response(raw_output)

        if verbose and parsed["success"]:
            print("✅ 改善案生成完了\n")
            print("=" * 50)
            print(parsed["analysis"])
            print("=" * 50)

        return parsed

    # ----------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------

    def _parse_response(self, raw: str) -> dict:
        """AIレスポンスから各セクションを抽出"""
        result = {
            "success": False,
            "raw": raw,
            "analysis": "",
            "code": "",
            "params_explanation": "",
            "risks": "",
            "error": None,
        }

        try:
            # <analysis>
            analysis_match = re.search(r"<analysis>(.*?)</analysis>", raw, re.DOTALL)
            result["analysis"] = analysis_match.group(1).strip() if analysis_match else ""

            # <strategy_code> → コードブロック抽出
            code_section_match = re.search(r"<strategy_code>(.*?)</strategy_code>", raw, re.DOTALL)
            if code_section_match:
                code_block = code_section_match.group(1)
                code_match = re.search(r"```python(.*?)```", code_block, re.DOTALL)
                result["code"] = code_match.group(1).strip() if code_match else code_block.strip()
            
            if not result["code"]:
                # フォールバック: レスポンス全体からコードブロックを探す
                code_match = re.search(r"```python(.*?)```", raw, re.DOTALL)
                result["code"] = code_match.group(1).strip() if code_match else ""

            # <params_explanation>
            params_match = re.search(r"<params_explanation>(.*?)</params_explanation>", raw, re.DOTALL)
            result["params_explanation"] = params_match.group(1).strip() if params_match else ""

            # <risks>
            risks_match = re.search(r"<risks>(.*?)</risks>", raw, re.DOTALL)
            result["risks"] = risks_match.group(1).strip() if risks_match else ""

            if not result["code"]:
                result["error"] = "コードブロックが見つかりませんでした"
                return result

            result["success"] = True
        except Exception as e:
            result["error"] = f"レスポンスのパースエラー: {e}"

        return result

    def _validate_and_load(self, code: str) -> dict:
        """
        生成コードを安全にロードして検証する。
        - 危険なimportがないかチェック
        - 構文エラーがないかチェック
        - BaseStrategyを継承しているかチェック
        - インスタンス化できるかチェック
        """
        # ホワイトリスト方式: 許可されたモジュールのみimport可能
        _ALLOWED_MODULES = {
            "numpy", "np", "pandas", "pd", "math", "statistics",
            "dataclasses", "typing", "enum", "collections",
            "datetime", "decimal", "functools", "itertools",
        }
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                # import文のチェック
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_root = alias.name.split(".")[0]
                        if module_root not in _ALLOWED_MODULES:
                            return {
                                "success": False,
                                "error": f"セキュリティチェック: 許可されていないimport '{alias.name}'（許可: {', '.join(sorted(_ALLOWED_MODULES))}）",
                                "strategy_class": None
                            }
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        module_root = node.module.split(".")[0]
                        if module_root not in _ALLOWED_MODULES:
                            return {
                                "success": False,
                                "error": f"セキュリティチェック: 許可されていないfrom import '{node.module}'（許可: {', '.join(sorted(_ALLOWED_MODULES))}）",
                                "strategy_class": None
                            }
                # 危険な組み込み関数の直接呼び出しを禁止
                elif isinstance(node, ast.Call):
                    _BANNED_FUNCTIONS = {
                        "eval", "exec", "compile", "__import__",
                        "getattr", "setattr", "delattr",
                        "open", "input", "breakpoint",
                        "globals", "locals", "vars",
                    }
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in _BANNED_FUNCTIONS:
                        return {
                            "success": False,
                            "error": f"セキュリティチェック: 禁止関数 '{func.id}()' の呼び出しを検出",
                            "strategy_class": None
                        }
                    # os.system(), os.remove() 等のメソッド呼び出しを検出
                    # ※ os/subprocess はimportホワイトリストでもブロック済み（二重防御）
                    if isinstance(func, ast.Attribute):
                        _BANNED_METHODS = {
                            "system", "popen", "check_output",
                            "Popen", "remove", "rmdir", "unlink",
                        }
                        if func.attr in _BANNED_METHODS:
                            return {
                                "success": False,
                                "error": f"セキュリティチェック: 禁止メソッド '.{func.attr}()' の呼び出しを検出",
                                "strategy_class": None
                            }
        except SyntaxError:
            pass  # 次の構文チェックで捕捉される

        # 構文チェック
        try:
            ast.parse(code)
        except SyntaxError as e:
            return {
                "success": False,
                "error": f"構文エラー: {e}",
                "strategy_class": None
            }

        # 一時ファイルに書き出してインポート
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, dir="/tmp"
            ) as f:
                f.write(code)
                tmp_path = f.name

            spec = importlib.util.spec_from_file_location("generated_strategy", tmp_path)
            module = importlib.util.module_from_spec(spec)
            # builtinsを制限して危険な関数へのアクセスを遮断（ASTチェックの二重防御）
            import builtins as _builtins
            safe_builtins = {k: v for k, v in vars(_builtins).items()
                            if k not in ("open", "exec", "eval", "compile",
                                         "input", "breakpoint",
                                         "globals", "locals", "vars",
                                         "getattr", "setattr", "delattr",
                                         "__import__")}
            # __import__はホワイトリスト付きラッパーに差し替え
            # __builtins__["__import__"](...) 経由のAST回避を防ぐ
            _real_import = _builtins.__import__
            _IMPORT_WHITELIST = {
                "numpy", "pandas", "math", "statistics",
                "dataclasses", "typing", "enum", "collections",
                "datetime", "decimal", "functools", "itertools",
                "plugins",  # BaseStrategy読み込み用
            }
            def _safe_import(name, *args, **kwargs):
                root = name.split(".")[0]
                if root not in _IMPORT_WHITELIST:
                    raise ImportError(f"セキュリティ: '{name}' のimportは許可されていません")
                return _real_import(name, *args, **kwargs)
            safe_builtins["__import__"] = _safe_import
            module.__builtins__ = safe_builtins
            sys.modules["generated_strategy"] = module
            spec.loader.exec_module(module)

            # GeneratedStrategyクラスを探す
            strategy_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and
                    attr_name != "BaseStrategy" and
                    hasattr(attr, "generate_signals")):
                    strategy_class = attr
                    break

            if strategy_class is None:
                return {
                    "success": False,
                    "error": "GeneratedStrategyクラスが見つかりません",
                    "strategy_class": None
                }

            # インスタンス化テスト
            instance = strategy_class()
            is_valid, errors = instance.validate()
            if not is_valid:
                return {
                    "success": False,
                    "error": f"戦略バリデーションエラー: {errors}",
                    "strategy_class": None
                }

            os.unlink(tmp_path)
            return {"success": True, "strategy_class": strategy_class, "error": None}

        except Exception as e:
            return {
                "success": False,
                "error": f"コードロードエラー: {e}\n{traceback.format_exc()}",
                "strategy_class": None
            }

    def _save_strategy(self, code: str, strategy, origin_prompt: str) -> Path:
        """戦略コードをファイルに保存"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^a-z0-9_]", "_", strategy.meta.name.lower())
        dir_name = f"{safe_name}_{timestamp}"
        strategy_dir = self.strategies_dir / dir_name
        strategy_dir.mkdir(parents=True, exist_ok=True)

        # 戦略コード本体
        strategy_file = strategy_dir / "strategy.py"
        strategy_file.write_text(code)

        # メタデータ
        meta_file = strategy_dir / "meta.json"
        meta_file.write_text(json.dumps({
            **strategy.to_dict(),
            "origin_prompt": origin_prompt,
        }, ensure_ascii=False, indent=2))

        return strategy_file

    def _register_strategy(self, strategy, file_path: Path, origin_prompt: str):
        """グローバルレジストリに登録"""
        registry = self._load_registry()
        entry = {
            **strategy.to_dict(),
            "file_path": str(file_path),
            "origin_prompt": origin_prompt,
            "backtest_results": [],
            "capital_allocated": 0,
        }
        registry.append(entry)
        self.registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2)
        )

    def _ensure_registry(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self.registry_path.write_text("[]")

    def _load_registry(self) -> list:
        return json.loads(self.registry_path.read_text())

    def list_strategies(self) -> list:
        """登録済み戦略の一覧を返す"""
        return self._load_registry()

    def get_strategy_code(self, strategy_name: str) -> Optional[str]:
        """戦略名からコードを取得"""
        registry = self._load_registry()
        for entry in registry:
            if strategy_name in entry.get("name", ""):
                file_path = Path(entry["file_path"])
                if file_path.exists():
                    return file_path.read_text()
        return None

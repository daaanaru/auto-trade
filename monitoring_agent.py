import sys
import os
import time
import random
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Add the automation directory to path to import LocalLLMClient
sys.path.append(os.environ.get("AUTOMATION_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "00_本陣", "automation")))
from llm_client import LocalLLMClient

import requests

# Discord Webhook URL（.envから読み込み）
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

class InvestmentMonitor:
    def __init__(self, model="qwen2.5:7b"):
        self.client = LocalLLMClient(model=model)
        self.market_data = {
            "BTC/USDT": {"price": 65000, "change_24h": "+2.5%"},
            "ETH/USDT": {"price": 3500, "change_24h": "-1.2%"},
            "SOL/USDT": {"price": 145, "change_24h": "+5.8%"}
        }

    def send_discord_notification(self, message):
        if not DISCORD_WEBHOOK_URL:
            return
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        except Exception as e:
            print(f"Error sending Discord notification: {e}")

    def get_simulated_market_state(self):
        # Simulate small price fluctuations
        for asset in self.market_data:
            change = random.uniform(-0.005, 0.005)
            self.market_data[asset]["price"] *= (1 + change)
        return self.market_data

    def analyze_market_with_ai(self):
        data_str = str(self.get_simulated_market_state())
        prompt = f"""
現在の仮想通貨市場（シミュレーション）のデータ：
{data_str}

エンジニア兼投資家として、このデータから注目べき動きや、
「攻め」のタイミングを示唆する兆候があるか分析してください。
もしチャンスがあれば報告してください。
出力は日本語で。
分析結果のみ、簡潔に答えて。
"""
        try:
            analysis = self.client.generate(prompt, system_prompt="あなたは投資戦略のアドバイザー（御庭番）です。冷静かつ鋭い分析を行ってください。")
            return analysis
        except Exception as e:
            return f"Error in AI Analysis: {e}"

    def run_cycle(self):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring cycle started...")
        analysis = self.analyze_market_with_ai()
        print(f"AI Analysis Result:\n{analysis}")
        
        # Always log
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitoring_log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now().isoformat()} ---\n")
            f.write(analysis + "\n\n")

        # Notify if substantial
        if "特記事項なし" not in analysis:
            self.send_discord_notification(f"📊 **Investment AI Analysis**\n{analysis}")

if __name__ == "__main__":
    monitor = InvestmentMonitor()
    monitor.run_cycle()

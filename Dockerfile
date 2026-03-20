FROM python:3.11-slim

WORKDIR /app

# システム依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python依存パッケージ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコード
COPY engine.py .
COPY optimize.py .
COPY strategies/ strategies/
COPY plugins/ plugins/
COPY optimized_params.json .
COPY us_stock_tickers.json .
COPY fx_tickers.json .

# バックテスト実行
# 使い方:
#   docker build -t auto-trade-backtest .
#   docker run auto-trade-backtest python3 optimize.py --strategy monthly --walk-forward
#   docker run auto-trade-backtest python3 engine.py --ticker 6758.T --strategy monthly
ENTRYPOINT ["python3"]
CMD ["optimize.py", "--help"]

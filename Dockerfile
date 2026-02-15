FROM python:3.11-slim

WORKDIR /app

# 依存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY . .

# Cloud Run は 8080 を待つ
ENV PORT=8080
EXPOSE 8080

# ここが最重要：起動を固定
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Технический порт — самому боту он не нужен (бот не принимает
# входящие запросы, только сам стучится в Telegram), но Timeweb Cloud
# ожидает какой-то порт по умолчанию для веб-приложений.
EXPOSE 8080

CMD ["python", "bot.py"]

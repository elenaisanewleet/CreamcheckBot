FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Аналитика и логи живут здесь. Подключи том, иначе статистика
# будет обнуляться при каждом пересоздании контейнера:
#   docker run -v creamcheck-data:/app/data ...
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1
ENV ANALYTICS_DB=/app/data/analytics.db
ENV LOG_FILE=/app/data/bot.log

# Веб-дашборд и health-проверка
EXPOSE 10000

CMD ["python", "-m", "bot.bot"]

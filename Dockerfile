FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/users.db

WORKDIR /app

# Dockerfile лежит в корне намеренно: иначе Railway видит index.html и
# автоматически собирает статический Caddy-образ вместо Python-бота.
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/bot.py .

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "bot.py"]

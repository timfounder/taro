FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/users.db

WORKDIR /app

COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/bot.py ./bot.py

RUN mkdir -p /data

# HTTP-API для Mini App (проверка подписки + напоминания). Railway
# передаёт фактический порт через переменную PORT.
EXPOSE 8080

CMD ["python", "bot.py"]

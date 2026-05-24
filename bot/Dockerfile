FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/users.db

WORKDIR /app

# Пути даны от корня репозитория, чтобы образ собирался и при
# `docker build -f bot/Dockerfile .` из корня, и на Railway (там
# контекст сборки — всегда корень репо).
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/bot.py .

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "bot.py"]

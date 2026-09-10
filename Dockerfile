FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py store.py pakasir.py web.py telegram_auth.py startup.sh ./
COPY templates/ ./templates/
COPY public/ ./public/

CMD ["sh", "startup.sh"]

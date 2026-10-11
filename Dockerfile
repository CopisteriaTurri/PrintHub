FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir --no-warn-script-location -r requirements.txt

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=builder /install /usr/local
COPY . .
# collectstatic non tocca il database: SECRET_KEY fittizia solo per questo passo.
RUN SECRET_KEY=build python manage.py collectstatic --noinput -v0 \
    && useradd --system --uid 1000 printhub \
    && mkdir -p /app/media && chown printhub /app/media
USER printhub
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
# Un solo server per HTTP e WebSocket. Il numero di processi lo legge uvicorn da
# WEB_CONCURRENCY (default 1; più di 1 richiede CHANNEL_LAYER_URL).
# --proxy-headers: dietro cloudflared lo schema reale (https/wss) arriva negli
# header X-Forwarded-*.
CMD ["uvicorn", "printhub.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

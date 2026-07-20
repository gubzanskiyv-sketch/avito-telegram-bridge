FROM python:3.12-slim

WORKDIR /app
COPY avito_telegram_bridge.py register_avito_webhooks.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "avito_telegram_bridge.py"]


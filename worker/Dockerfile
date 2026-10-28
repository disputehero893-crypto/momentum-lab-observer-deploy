FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOMENTUM_MODE=demo \
    PAPER_TRADING_ENABLED=false \
    HOST=0.0.0.0 \
    PORT=10000 \
    JOURNAL_PATH=/var/data/momentum.sqlite3 \
    EXPORT_DIR=/var/data/exports

# zoneinfo needs the exchange time-zone database; there are no pip dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY momentum ./momentum
COPY examples ./examples
COPY bin/serve-observe /usr/local/bin/serve-observe
RUN chmod 0755 /usr/local/bin/serve-observe && mkdir -p /var/data/exports && chown -R 10001:10001 /var/data
EXPOSE 10000
ENTRYPOINT ["python", "-m", "momentum.container_entrypoint"]
CMD ["serve"]


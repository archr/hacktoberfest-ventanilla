FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    AUTO_INGEST=true
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home ventanilla \
    && mkdir /app/data && chown ventanilla:ventanilla /app/data
COPY app ./app
COPY mock_doc_service ./mock_doc_service
COPY config ./config
COPY scripts/container-start.sh ./scripts/container-start.sh
USER ventanilla
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=3)"
ENTRYPOINT ["sh", "/app/scripts/container-start.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8700", "--workers", "1", "--no-proxy-headers"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
RUN useradd --create-home --uid 10001 contared \
    && mkdir -p /var/lib/contared/private_storage/import_previews \
        /var/lib/contared/private_storage/direct_reconciliation \
    && chown -R contared:contared /app /var/lib/contared

ENV IMPORT_PREVIEW_STORAGE_DIR=/var/lib/contared/private_storage/import_previews \
    DIRECT_RECONCILIATION_STORAGE_DIR=/var/lib/contared/private_storage/direct_reconciliation
USER contared
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/status/', timeout=5)"
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "1", "--timeout", "600", "config.wsgi:application"]

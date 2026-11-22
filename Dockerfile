FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE=/app/data/warehouse.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY warehouse ./warehouse
COPY app.py ./

RUN mkdir -p /app/data && useradd --create-home --uid 10001 warehouse \
    && chown -R warehouse:warehouse /app/data
USER warehouse

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-2} --access-logfile - app:app"]

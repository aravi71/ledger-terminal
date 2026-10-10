FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_HOST=0.0.0.0 \
    LEDGER_PORT=3002 \
    LEDGER_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY defaults ./defaults
COPY run.py .

RUN useradd --create-home ledger && mkdir -p /data && chown ledger /data
USER ledger
VOLUME ["/data"]
EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:3002/healthz'); sys.exit(0)"
CMD ["python", "run.py"]

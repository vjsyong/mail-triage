FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py store.py engine.py app.py ./

ENV DATA_DIR=/data \
    UI_HOST=0.0.0.0 \
    UI_PORT=8097

EXPOSE 8097

HEALTHCHECK --interval=60s --timeout=6s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8097/healthz', timeout=5).status==200 else 1)"

CMD ["python", "app.py"]

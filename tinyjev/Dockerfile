# TinyJev classifier sidecar (CPU, self-contained).
# Unlike the fusion sidecar, the model is NOT baked into the image: the first
# boot downloads TinyJev-0.6B from HuggingFace into the mounted cache volume
# (mount a volume at /data/hf or the download repeats on every recreate), then
# loads it and warms up. The HTTP port opens immediately; /healthz returns 503
# (with the current phase + download progress) until the model is ready.
# Build:  docker build -t mail-triage-tinyjev tinyjev/
# Run:    see docker-compose.yml in this directory.
FROM python:3.12-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf \
    TINYJEV_MODEL=TinyJev-0.6B \
    TINYJEV_DEVICE=cpu \
    TINYJEV_NR=on \
    PORT=8099

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "tinyjev[torch]"

COPY server.py /app/server.py

# Runtime download goes here; keep it on a volume so it survives recreation.
VOLUME ["/data/hf"]

EXPOSE 8099
CMD ["python", "/app/server.py"]

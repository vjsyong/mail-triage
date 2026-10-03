# TinyJev + MiniCPM fusion sidecar (CPU, self-contained).
# The model is baked into the image and loaded offline, so startup is just
# torch/transformers import + weights + warm-up (~8s on this host); the HTTP
# port opens immediately and /healthz reports 503 until the model is ready.
# Build:  docker build -t mail-triage-fusion fusion/
# Run:    see docker-compose.yml in this directory.
FROM python:3.12-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf \
    TINYJEV_MODEL=TinyJev-0.6B \
    TINYJEV_DEVICE=cpu \
    PORT=8098

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "tinyjev[torch]" \
 && python -c "from huggingface_hub import snapshot_download; snapshot_download('AnkitAI/TinyJev-0.6B')"

# Cache is complete: never phone home at runtime.
ENV HF_HUB_OFFLINE=1

COPY server.py /app/server.py

EXPOSE 8098
CMD ["python", "/app/server.py"]

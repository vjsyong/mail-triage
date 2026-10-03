# TinyJev + MiniCPM fusion sidecar (CPU, self-contained).
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
 && pip install --no-cache-dir "tinyjev[torch]"

COPY server.py /app/server.py

VOLUME ["/data/hf"]
EXPOSE 8098
CMD ["python", "/app/server.py"]

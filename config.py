"""Configuration for Mail Triage (environment-driven).

Secrets and connection details come from the environment (.env via docker compose);
everything user-tunable lives in the SQLite settings table instead.
"""
import os

def get(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


IMAP_HOST = get("IMAP_HOST", "100.93.139.49")
IMAP_PORT = int(get("IMAP_PORT", "1993"))
IMAP_USER = get("IMAP_USER", "")
IMAP_PASSWORD = get("IMAP_PASSWORD", "")
# The email-oauth2-proxy expects PLAIN local connections (it secures the far side).
# Keep this off unless you point the app at a TLS-capable server.
IMAP_TLS = get("IMAP_TLS", "0") == "1"

LLM_BASE_URL = get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_API_KEY = get("LLM_API_KEY", "")
LLM_MODEL = get("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(get("LLM_TIMEOUT", "90"))
# Optional fallback endpoint, used only when the primary endpoint fails
# (e.g. keep a cloud model as a safety net when the local GPU server is down).
LLM_FALLBACK_BASE_URL = get("LLM_FALLBACK_BASE_URL", "")
LLM_FALLBACK_API_KEY = get("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL = get("LLM_FALLBACK_MODEL", "")

# RAG: local embedding + rerank servers (see embed/, TEI on GPU 1).
EMBED_BASE_URL = get("EMBED_BASE_URL", "").rstrip("/")      # e.g. http://100.93.139.49:8041
EMBED_MODEL = get("EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
EMBED_TIMEOUT = int(get("EMBED_TIMEOUT", "180"))
RERANK_BASE_URL = get("RERANK_BASE_URL", "").rstrip("/")    # e.g. http://100.93.139.49:8042
RERANK_MODEL = get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
RERANK_TIMEOUT = int(get("RERANK_TIMEOUT", "90"))

UI_HOST = get("UI_HOST", "0.0.0.0")
UI_PORT = int(get("UI_PORT", "8097"))

DATA_DIR = get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "triage.db")

APP_NAME = "Mail Triage"

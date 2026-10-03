#!/usr/bin/env python3
"""TinyJev + MiniCPM fusion sidecar for Mail Triage.

The "fusion system" measured on the v2 classification suite:
  category   : TinyJev-0.6B (Qwen3-0.6B + pointer head, Choice over the app's
               categories with one-line descriptions) - CPU friendly.
  needs_reply: MiniCPM5-2B, *direct* needs_reply prompt, thinking OFF (the
               recall-oriented wording; see RECALL_SYSTEM). This is the tune
               that took misses from 16 -> 2 on the acceptance split.
  prose      : the 2B's summary/reason.

POST /classify  {"state": "<email text>", "categories": ["Action", ...]}
  -> {"category", "category_confidence", "category_probabilities",
      "needs_reply", "needs_reply_confidence", "summary", "reason",
      "components", "latency_ms"}

The 2B endpoint is any OpenAI-compatible server (llama.cpp / vLLM):
  FUSION_LLM_BASE_URL=http://host.docker.internal:8042/v1
  FUSION_LLM_MODEL=minicpm5-2b
Optional: FUSION_LLM_API_KEY, FUSION_USER_NAME (default "Sean"),
TINYJEV_MODEL (default "TinyJev-0.6B"), TINYJEV_DEVICE (default "cpu"),
PORT (default 8098), HF_HOME for the model cache.

Models stay loaded; the service is single-purpose and local. It never writes
anything and never sees credentials beyond the optional LLM API key.
"""
import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CATEGORIES = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"]
DESC = {
    "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
    "Notification": "Automatic status updates and notices that need no action",
    "Newsletter": "Recurring editorial or digest content the owner subscribed to",
    "Receipt": "Order confirmations, invoices, and payment receipts",
    "Personal": "Mail from friends, family, or personal contacts",
    "Promo": "Marketing, offers, and sales promotions",
}

RECALL_SYSTEM = (
    "You triage incoming email for {user}. The category has already been decided. "
    "Reply with a single JSON object and nothing else. Shape: "
    '{{"needs_reply": true|false, "confidence": 0.0-1.0, '
    '"summary": "one short sentence saying what the email is", '
    '"reason": "why, max 15 words"}}. '
    "Decide needs_reply by whether the sender expects {user} to respond or act: true for any "
    "request, question, invitation, or task addressed to {user} (including short personal "
    "notes); false only for purely informational or automated mail (notifications, receipts, "
    "newsletters, marketing). When unsure, prefer true."
)

LLM_BASE = (os.environ.get("FUSION_LLM_BASE_URL") or "").rstrip("/")
LLM_MODEL = os.environ.get("FUSION_LLM_MODEL") or "minicpm5-2b"
LLM_KEY = os.environ.get("FUSION_LLM_API_KEY") or ""
USER_NAME = os.environ.get("FUSION_USER_NAME") or "Sean"
TINYJEV_MODEL = os.environ.get("TINYJEV_MODEL") or "TinyJev-0.6B"
TINYJEV_DEVICE = os.environ.get("TINYJEV_DEVICE") or "cpu"
PORT = int(os.environ.get("PORT") or 8098)
LLM_TIMEOUT = float(os.environ.get("FUSION_LLM_TIMEOUT") or 60)

_lock = threading.Lock()
_agent = None


def _llm_call(state):
    """One direct needs_reply completion against the 2B, thinking off."""
    if not LLM_BASE:
        raise RuntimeError("FUSION_LLM_BASE_URL is not configured")
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.0,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": RECALL_SYSTEM.format(user=USER_NAME)},
            {"role": "user", "content": state},
        ],
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    optional = ["response_format", "chat_template_kwargs"]
    req = urllib.request.Request(
        LLM_BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + LLM_KEY} if LLM_KEY else {})},
        method="POST")
    for _ in range(len(optional) + 1):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 422) and optional:
                payload.pop(optional.pop(0), None)
                req.data = json.dumps(payload).encode("utf-8")
                continue
            raise
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    try:
        parsed = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        parsed = json.loads(content[start:end + 1]) if 0 <= start < end else {}
    return parsed


def classify(state, categories):
    cats = [c for c in (categories or CATEGORIES) if isinstance(c, str) and c.strip()]
    cats = cats or CATEGORIES
    criteria = {c: DESC.get(c, "") for c in cats}
    t0 = time.perf_counter()
    with _lock:
        out = _agent.predict({
            "state": state,
            "questions": {
                "category": {
                    "type": "choice",
                    "instructions": "Which category does this email belong to?",
                    "criteria": criteria,
                }
            },
        })
        tiny_ms = out["execution"]["model_ms"]
        ans = out["states"][0]["answers"]["category"]
        result = {
            "category": ans["choice"],
            "category_confidence": float(ans["confidence"]),
            "category_probabilities": ans["probabilities"],
            "components": {"tinyjev_ms": round(tiny_ms, 1), "tinyjev_model": TINYJEV_MODEL},
        }
        error = None
        try:
            llm = _llm_call(state)
            result.update({
                "needs_reply": bool(llm.get("needs_reply")),
                "needs_reply_confidence": llm.get("confidence"),
                "summary": llm.get("summary") or "",
                "reason": llm.get("reason") or "",
            })
            result["components"]["llm_ms"] = None
        except Exception as exc:  # noqa: BLE001
            error = "%s: %s" % (type(exc).__name__, exc)
            result.update({"needs_reply": None, "needs_reply_confidence": None,
                           "summary": "", "reason": ""})
        result["error"] = error
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/healthz", "/health"):
            self._send(200, {"ok": True, "model": TINYJEV_MODEL, "llm": LLM_BASE or None,
                             "llm_model": LLM_MODEL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/classify":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
            state = str(body.get("state") or "")
            if not state.strip():
                self._send(400, {"error": "state is required"})
                return
            self._send(200, classify(state[:20000], body.get("categories")))
        except Exception:  # noqa: BLE001
            self._send(500, {"error": traceback.format_exc(limit=3)})

    def log_message(self, fmt, *args):
        pass


def main():
    global _agent
    t0 = time.time()
    import tinyjev
    _agent = tinyjev.load(TINYJEV_MODEL, device=TINYJEV_DEVICE)
    _agent.predict({"state": "warmup", "questions": {
        "category": {"type": "choice", "instructions": "category?",
                     "criteria": {CATEGORIES[0]: DESC[CATEGORIES[0]],
                                  CATEGORIES[1]: DESC[CATEGORIES[1]]}}}})
    print("fusion ready in %.1fs (tinyjev=%s device=%s llm=%s model=%s)"
          % (time.time() - t0, TINYJEV_MODEL, TINYJEV_DEVICE, LLM_BASE or "-", LLM_MODEL), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

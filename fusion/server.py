#!/usr/bin/env python3
"""TinyJev + MiniCPM fusion sidecar for Mail Triage.

The system is named MiniCPM5-2B-TinyJev-Fusion (FUSION_NAME). It was measured
on the v2 classification suite as:
  category   : TinyJev-0.6B (Qwen3-0.6B + pointer head, Choice over the app's
               categories with one-line descriptions) - CPU friendly.
  needs_reply: MiniCPM5-2B, *direct* needs_reply prompt, thinking OFF (the
               recall-oriented wording; see RECALL_SYSTEM). This is the tune
               that took misses from 16 -> 2 on the acceptance split.
  prose      : the 2B's summary/reason.

Two faces, same process:

  POST /classify   fusion-native API (used by the Fusion Lab plugin)
    {"state", "categories"} -> {"category", "category_confidence", ...}

  /v1/*            OpenAI-compatible facade, *transparent*: point any client's
                   base URL here. Chat requests whose system prompt is the
                   production classify prompt are answered by the fusion
                   (TinyJev category + 2B needs_reply); every other chat request
                   is proxied verbatim to the backing LLM (streaming included).
                   No app-side or plugin-side changes are required.

The 2B endpoint is any OpenAI-compatible server (llama.cpp / vLLM):
  FUSION_LLM_BASE_URL=http://host.docker.internal:8042/v1
  FUSION_LLM_MODEL=minicpm5-2b
Optional: FUSION_LLM_API_KEY, FUSION_USER_NAME (default "Sean"),
TINYJEV_MODEL (default "TinyJev-0.6B"), TINYJEV_DEVICE (default "cpu"),
PORT (default 8098), HF_HOME for the model cache.

Modes (FUSION_NR_MODE): llm = 2B answers needs_reply (default); cascade =
local false for FUSION_CASCADE_CATEGORIES, 2B for the rest; tiny = TinyJev's
Noul head answers in the same forward pass (no generation - Potato/Pi mode).
FUSION_NR_THRESHOLD (default 0.40) sets the Noul decision threshold.

Models stay loaded; the service is single-purpose and local. It never writes
anything and never sees credentials beyond the optional LLM API key.
"""
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
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

CLASSIFY_MARK = "triage incoming email for"

FUSION_NAME = os.environ.get("FUSION_NAME") or "MiniCPM5-2B-TinyJev-Fusion"
LLM_BASE = (os.environ.get("FUSION_LLM_BASE_URL") or "").rstrip("/")
LLM_MODEL = os.environ.get("FUSION_LLM_MODEL") or "minicpm5-2b"
LLM_KEY = os.environ.get("FUSION_LLM_API_KEY") or ""
USER_NAME = os.environ.get("FUSION_USER_NAME") or "Sean"
TINYJEV_MODEL = os.environ.get("TINYJEV_MODEL") or "TinyJev-0.6B"
TINYJEV_DEVICE = os.environ.get("TINYJEV_DEVICE") or "cpu"
PORT = int(os.environ.get("PORT") or 8098)
LLM_TIMEOUT = float(os.environ.get("FUSION_LLM_TIMEOUT") or 60)
# needs_reply engine:
#   llm     - ask the 2B (full recall prompt); best quality, slowest
#   cascade - answer false locally for categories that rarely need replies,
#             ask the 2B only for the rest (measured: 41% of calls, best F1)
#   tiny    - TinyJev's Noul head answers in the same forward pass; zero
#             generation, Potato/Raspberry-Pi friendly, ~half the replies found
NR_MODE = (os.environ.get("FUSION_NR_MODE") or "llm").strip().lower()
NR_THRESHOLD = float(os.environ.get("FUSION_NR_THRESHOLD") or 0.40)
CASCADE_CATEGORIES = {c.strip() for c in
                      (os.environ.get("FUSION_CASCADE_CATEGORIES")
                       or "Notification,Newsletter,Receipt,Promo").split(",") if c.strip()}
NOUL_INSTRUCTIONS = "Does this email need a reply from the account owner?"

_lock = threading.Lock()
_agent = None
_ready = threading.Event()


# ---------------------------------------------------------------- pure helpers

def is_classification(messages):
    """True when the request carries the production classify system prompt."""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        text = str(m.get("content") or "")
        if CLASSIFY_MARK in text and '"category"' in text and "one of" in text:
            return True
    return False


def extract_categories(messages):
    """Pull the category list out of the production prompt: "one of [A, B, ...]"."""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        mt = re.search(r"one of \[([^\]]+)\]", str(m.get("content") or ""))
        if mt:
            return [c.strip().strip('"').strip("'") for c in mt.group(1).split(",")
                    if c.strip().strip('"').strip("'")]
    return []


def cascade_skip(category):
    """Categories whose mail is answered needs_reply=false without the 2B."""
    return str(category or "") in CASCADE_CATEGORIES


def noul_decision(p_true, threshold=None):
    """TinyJev's Noul head -> (needs_reply, confidence)."""
    threshold = NR_THRESHOLD if threshold is None else threshold
    p = float(p_true)
    return bool(p >= threshold), round(max(p, 1.0 - p), 3)


def upstream_model(requested):
    """Clients may target the fusion alias for every call; proxying maps it back."""
    req = str(requested or "").strip()
    if not req or req == FUSION_NAME:
        return LLM_MODEL
    return req


def last_user(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def verdict_content(result):
    """Production classify JSON from a fusion result; None when needs_reply failed."""
    if not result or result.get("needs_reply") is None:
        return None
    nr_conf = result.get("needs_reply_confidence")
    try:
        nr_conf = float(nr_conf)
    except (TypeError, ValueError):
        nr_conf = float(result.get("category_confidence") or 0.0)
    return {
        "category": result.get("category"),
        "needs_reply": bool(result.get("needs_reply")),
        "confidence": round(min(float(result.get("category_confidence") or 0.0),
                                max(0.0, min(1.0, nr_conf))), 3),
        "summary": result.get("summary") or "",
        "reason": result.get("reason") or "",
    }


def openai_completion(content, model, usage=None):
    return {
        "id": "chatcmpl-fusion-" + uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "fusion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _est_tokens(text):
    return max(1, len(str(text or "")) // 4)


# -------------------------------------------------------------------- backends

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
    data = None
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
    if data is None:
        raise RuntimeError("LLM returned no response")
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    try:
        return json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start:end + 1]) if 0 <= start < end else {}


def classify(state, categories):
    cats = [c for c in (categories or CATEGORIES) if isinstance(c, str) and c.strip()]
    cats = cats or CATEGORIES
    criteria = {c: DESC.get(c, "") for c in cats}
    questions = {"category": {
        "type": "choice",
        "instructions": "Which category does this email belong to?",
        "criteria": criteria,
    }}
    if NR_MODE == "tiny":
        questions["needs_reply"] = {"type": "noul", "instructions": NOUL_INSTRUCTIONS}
    t0 = time.perf_counter()
    with _lock:
        out = _agent.predict({"state": state, "questions": questions})
        tiny_ms = out["execution"]["model_ms"]
        ans = out["states"][0]["answers"]
        cat_ans = ans["category"]
        cat = cat_ans["choice"]
        result = {
            "model": FUSION_NAME,
            "category": cat,
            "category_confidence": float(cat_ans["confidence"]),
            "category_probabilities": cat_ans["probabilities"],
            "components": {"tinyjev_ms": round(tiny_ms, 1), "tinyjev_model": TINYJEV_MODEL,
                           "llm_ms": None, "nr_mode": NR_MODE},
        }
        error = None
        if NR_MODE == "tiny":
            p_true = float(ans["needs_reply"]["p_true"])
            nr, conf = noul_decision(p_true)
            result.update({"needs_reply": nr, "needs_reply_confidence": conf,
                           "summary": "", "reason": "TinyJev Noul decision"})
            result["components"]["tinyjev_nr_p_true"] = round(p_true, 3)
        elif NR_MODE == "cascade" and cascade_skip(cat):
            result.update({"needs_reply": False, "needs_reply_confidence": None,
                           "summary": "",
                           "reason": "category gate: %s rarely needs replies" % cat})
            result["components"]["nr_source"] = "cascade"
        else:
            try:
                llm = _llm_call(state)
                result.update({
                    "needs_reply": bool(llm.get("needs_reply")),
                    "needs_reply_confidence": llm.get("confidence"),
                    "summary": llm.get("summary") or "",
                    "reason": llm.get("reason") or "",
                })
                result["components"]["nr_source"] = "llm"
            except Exception as exc:  # noqa: BLE001
                error = "%s: %s" % (type(exc).__name__, exc)
                result.update({"needs_reply": None, "needs_reply_confidence": None,
                               "summary": "", "reason": ""})
        result["error"] = error
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing

    def _send(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")

    def _auth(self):
        if LLM_KEY:
            return {"Authorization": "Bearer " + LLM_KEY}
        got = self.headers.get("Authorization")
        return {"Authorization": got} if got else {}

    # ---------------------------------------------------------------- routes

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/healthz", "/health"):
            if _ready.is_set():
                self._send(200, {"ok": True, "model": TINYJEV_MODEL, "fusion": FUSION_NAME,
                                 "llm": LLM_BASE or None, "llm_model": LLM_MODEL,
                                 "nr_mode": NR_MODE})
            else:
                self._send(503, {"ok": False, "loading": True, "model": TINYJEV_MODEL})
        elif path == "/v1/models":
            self._models()
        else:
            self._send(404, {"error": "not found"})

    def _models(self):
        ids = []
        if LLM_BASE:
            try:
                req = urllib.request.Request(LLM_BASE + "/models", headers=self._auth())
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                if not ids:  # llama.cpp's Ollama-shaped list
                    ids = [m.get("model") or m.get("name")
                           for m in (data.get("models") or [])
                           if m.get("model") or m.get("name")]
            except Exception:  # noqa: BLE001
                pass
        if not ids:
            ids = [LLM_MODEL]
        ids = [FUSION_NAME] + [i for i in ids if i and i != FUSION_NAME]
        self._send(200, {"object": "list",
                         "data": [{"id": i, "object": "model", "owned_by": "fusion"}
                                  for i in ids]})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/classify":
            self._classify_route()
        elif path == "/v1/chat/completions":
            self._chat_route()
        else:
            self._send(404, {"error": "not found"})

    def _classify_route(self):
        if not _ready.is_set():
            self._send(503, {"error": "model still loading; retry in a few seconds"})
            return
        try:
            body = self._body()
            state = str(body.get("state") or "")
            if not state.strip():
                self._send(400, {"error": "state is required"})
                return
            self._send(200, classify(state[:20000], body.get("categories")))
        except Exception:  # noqa: BLE001
            self._send(500, {"error": traceback.format_exc(limit=3)})

    def _chat_route(self):
        try:
            raw = self._body()
        except Exception:  # noqa: BLE001
            self._send(400, {"error": {"message": "invalid JSON body", "type": "invalid_request"}})
            return
        messages = raw.get("messages") or []
        if is_classification(messages):
            self._fusion_chat(raw, messages)
            return
        self._proxy_chat(raw, bool(raw.get("stream")))

    def _fusion_chat(self, raw, messages):
        if not _ready.wait(30):
            self._send(503, {"error": {"message": "fusion model still loading",
                                       "type": "unavailable"}})
            return
        state = last_user(messages)[:20000]
        cats = extract_categories(messages) or None
        try:
            result = classify(state, cats)
        except Exception as exc:  # noqa: BLE001
            self._send(502, {"error": {"message": "fusion failed: %s" % exc,
                                       "type": "upstream_error"}})
            return
        content = verdict_content(result)
        if content is None:
            self._send(502, {"error": {"message": "fusion needs_reply half failed: %s"
                                       % (result.get("error") or "unknown"),
                                       "type": "upstream_error"}})
            return
        text = json.dumps(content, ensure_ascii=False)
        model = FUSION_NAME
        usage = {"prompt_tokens": _est_tokens(state), "completion_tokens": _est_tokens(text),
                 "total_tokens": _est_tokens(state) + _est_tokens(text)}
        if raw.get("stream"):
            self._send_sse_content(text, model, usage)
        else:
            self._send(200, openai_completion(text, model, usage))

    def _send_sse_content(self, text, model, usage):
        cid = "chatcmpl-fusion-" + uuid.uuid4().hex[:12]
        created = int(time.time())

        def frame(delta, finish=None):
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }, ensure_ascii=False) + "\n\n"

        body = (frame({"role": "assistant"}) + frame({"content": text}) +
                frame({}, "stop") +
                "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk",
                                       "created": created, "model": model,
                                       "choices": [], "usage": usage}) + "\n\n" +
                "data: [DONE]\n\n")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_chat(self, raw, stream):
        raw = dict(raw)
        raw["model"] = upstream_model(raw.get("model"))
        if not LLM_BASE:
            self._send(502, {"error": {"message": "FUSION_LLM_BASE_URL is not configured",
                                       "type": "upstream_error"}})
            return
        try:
            req = urllib.request.Request(
                LLM_BASE + "/chat/completions",
                data=json.dumps(raw).encode("utf-8"),
                headers={"Content-Type": "application/json", **self._auth()},
                method="POST")
            upstream = urllib.request.urlopen(req, timeout=LLM_TIMEOUT * 4)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as exc:  # noqa: BLE001
            self._send(502, {"error": {"message": "upstream LLM unreachable: %s" % exc,
                                       "type": "upstream_error"}})
            return
        ctype = upstream.headers.get("Content-Type") or "application/json"
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                self.close_connection = True
        else:
            data = upstream.read()
            self.send_response(upstream.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def _load():
    """Load TinyJev + warm up off the request path; /healthz flips to 200 when done."""
    global _agent
    t0 = time.time()
    import tinyjev
    _agent = tinyjev.load(TINYJEV_MODEL, device=TINYJEV_DEVICE)
    _agent.predict({"state": "warmup", "questions": {
        "category": {"type": "choice", "instructions": "category?",
                     "criteria": {CATEGORIES[0]: DESC[CATEGORIES[0]],
                                  CATEGORIES[1]: DESC[CATEGORIES[1]]}}}})
    _ready.set()
    print("fusion ready in %.1fs (tinyjev=%s device=%s llm=%s model=%s)"
          % (time.time() - t0, TINYJEV_MODEL, TINYJEV_DEVICE, LLM_BASE or "-", LLM_MODEL), flush=True)


def main():
    print("fusion loading (tinyjev=%s device=%s llm=%s model=%s)..."
          % (TINYJEV_MODEL, TINYJEV_DEVICE, LLM_BASE or "-", LLM_MODEL), flush=True)
    threading.Thread(target=_load, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

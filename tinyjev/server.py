#!/usr/bin/env python3
"""TinyJev classifier sidecar for Mail Triage.

Owns the whole TinyJev model lifecycle so the mt-tinyjev-classifier plugin
(QuickJS sandbox - no filesystem, no sockets) never has to:

  download   first boot pulls the weights from HuggingFace into the mounted
             cache volume (HF_HOME); progress is polled while it runs
  load       tinyjev.load() builds the agent, then one warm-up predict
  ready      /healthz flips to 200 and /classify answers

Endpoints:

  GET  /healthz   200 when ready, 503 with the current phase otherwise
  GET  /status    full lifecycle report (phase, download progress, timings)
  POST /load      (re)start download+load in the background; idempotent
  POST /classify  {"state", "categories", "nr"} -> one forward pass:
                  Choice over the categories (+ Noul needs_reply when nr=on)

The /classify response shape matches the fusion sidecar's, so the plugin can
point at either service unchanged. Everything is local: after the first
download the service is fully offline. It never writes anything except the
model cache and never sees credentials.

Env: PORT (8099), TINYJEV_MODEL (TinyJev-0.6B), TINYJEV_DEVICE (cpu),
TINYJEV_QUANTIZE ("" = fp16, or 4/8), TINYJEV_NR (on|off), TINYJEV_NR_THRESHOLD
(0.40), TINYJEV_THREADS (4, torch/OMP threads), HF_HOME (/data/hf).
"""
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT") or 8099)
TINYJEV_MODEL = os.environ.get("TINYJEV_MODEL") or "TinyJev-0.6B"
TINYJEV_DEVICE = os.environ.get("TINYJEV_DEVICE") or "cpu"
try:
    TINYJEV_QUANTIZE = int(os.environ.get("TINYJEV_QUANTIZE") or 0) or None
except ValueError:
    TINYJEV_QUANTIZE = None
NR_ON = (os.environ.get("TINYJEV_NR") or "on").strip().lower() not in ("off", "0", "no")
NR_THRESHOLD = float(os.environ.get("TINYJEV_NR_THRESHOLD") or 0.40)
try:
    THREADS = str(int(os.environ.get("TINYJEV_THREADS") or 4))
except ValueError:
    THREADS = "4"
os.environ.setdefault("OMP_NUM_THREADS", THREADS)
os.environ.setdefault("MKL_NUM_THREADS", THREADS)
HF_HOME = os.environ.get("HF_HOME") or os.path.expanduser(
    os.path.join("~", ".cache", "huggingface"))

CATEGORIES = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"]
DESC = {
    "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
    "Notification": "Automatic status updates and notices that need no action",
    "Newsletter": "Recurring editorial or digest content the owner subscribed to",
    "Receipt": "Order confirmations, invoices, and payment receipts",
    "Personal": "Mail from friends, family, or personal contacts",
    "Promo": "Marketing, offers, and sales promotions",
}
NOUL_INSTRUCTIONS = "Does this email need a reply from the account owner?"

# Fallback name -> (hf repo, revision) when tinyjev's own registry is absent.
_FALLBACK_REPOS = {"TinyJev-0.6B": ("AnkitAI/TinyJev-0.6B", None),
                   "TinyJev-4B-v1": ("AnkitAI/TinyJev-4B", "v1")}

_started = time.time()
_lifecycle_lock = threading.Lock()   # one lifecycle transition at a time
_state_lock = threading.Lock()       # guards the _lifecycle dict
_predict_lock = threading.Lock()     # the 0.6B answers one call at a time
_agent = None
_ready = threading.Event()
_worker_alive = False

_lifecycle = {
    "phase": "boot",         # boot | download | load | ready | error
    "progress": None,        # 0..1 download progress (None = unknown)
    "cached_bytes": 0,
    "total_bytes": None,
    "detail": "",
    "error": "",
    "model": TINYJEV_MODEL,
    "device": TINYJEV_DEVICE,
    "quantize": TINYJEV_QUANTIZE,
    "nr": "noul" if NR_ON else "off",
    "nr_threshold": NR_THRESHOLD,
    "hf_home": HF_HOME,
    "calls": 0,
    "since": int(_started),
    "ready_at": 0,
}


# ---------------------------------------------------------------- pure helpers

def noul_decision(p_true, threshold=None):
    """TinyJev's Noul head -> (needs_reply, confidence)."""
    threshold = NR_THRESHOLD if threshold is None else threshold
    p = float(p_true)
    return bool(p >= threshold), round(max(p, 1.0 - p), 3)


def hub_repo(model_name):
    """Model name -> (hf repo id, revision)."""
    try:
        from tinyjev.registry import resolve
        repo, _meta, rev = resolve(str(model_name or ""))
        if repo:
            return repo, rev
    except Exception:  # noqa: BLE001  (registry is a convenience, not a need)
        pass
    repo, rev = _FALLBACK_REPOS.get(str(model_name or ""), (str(model_name or ""), None))
    return repo, rev


def cache_dir():
    return os.path.join(HF_HOME, "hub")


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def total_bytes(repo, revision):
    """Best-effort expected download size, for the progress bar."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, revision=revision, files_metadata=True)
        size = sum((f.size or 0) for f in (info.siblings or []) if getattr(f, "size", None))
        return size or None
    except Exception:  # noqa: BLE001
        return None


def lifecycle():
    with _state_lock:
        snap = dict(_lifecycle)
    snap["uptime_s"] = int(time.time() - _started)
    snap["ready"] = _ready.is_set()
    if snap["progress"] is not None:
        snap["progress"] = round(min(max(float(snap["progress"]), 0.0), 1.0), 3)
    return snap


def _set_phase(phase, **kw):
    with _state_lock:
        _lifecycle["phase"] = phase
        for k, v in kw.items():
            _lifecycle[k] = v


def _watch_download(stop, total):
    """Poll the cache size while the download runs; update progress."""
    while not stop.wait(2.0):
        have = _dir_bytes(cache_dir())
        with _state_lock:
            _lifecycle["cached_bytes"] = have
            _lifecycle["total_bytes"] = total
            _lifecycle["progress"] = (have / total) if total else None


# ------------------------------------------------------------------- lifecycle

def _ensure(force=False):
    """Download (if needed) -> load -> warm up. Runs on a background thread."""
    global _agent, _worker_alive
    with _lifecycle_lock:
        if _ready.is_set() and _agent is not None and not force:
            return
        if _worker_alive:
            return
        _worker_alive = True
    try:
        t0 = time.time()
        repo, revision = hub_repo(TINYJEV_MODEL)
        # 1. weights present?
        cached = False
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repo, revision=revision, local_files_only=True)
            cached = True
        except Exception:  # noqa: BLE001
            cached = False
        if not cached:
            _set_phase("download", detail="fetching %s from HuggingFace" % repo,
                       error="", progress=None, total_bytes=None)
            total = total_bytes(repo, revision)
            with _state_lock:
                _lifecycle["total_bytes"] = total
            stop = threading.Event()
            watcher = threading.Thread(target=_watch_download, args=(stop, total), daemon=True)
            watcher.start()
            try:
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id=repo, revision=revision)
            finally:
                stop.set()
                watcher.join(timeout=5)
            with _state_lock:
                _lifecycle["cached_bytes"] = _dir_bytes(cache_dir())
                _lifecycle["progress"] = 1.0
        # 2. load the agent (cache-complete -> pure local read)
        _set_phase("load", detail="loading %s (%s)" % (TINYJEV_MODEL, TINYJEV_DEVICE),
                   error="")
        import tinyjev
        kwargs = {"device": TINYJEV_DEVICE}
        if TINYJEV_QUANTIZE:
            kwargs["quantize"] = TINYJEV_QUANTIZE
        agent = tinyjev.load(TINYJEV_MODEL, **kwargs)
        # 3. warm-up off the request path
        agent.predict({"state": "warmup", "questions": {
            "category": {"type": "choice", "instructions": "category?",
                         "criteria": {CATEGORIES[0]: DESC[CATEGORIES[0]],
                                      CATEGORIES[1]: DESC[CATEGORIES[1]]}}}})
        with _predict_lock:
            _agent = agent
        _ready.set()
        _set_phase("ready", detail="", error="", progress=1.0)
        with _state_lock:
            _lifecycle["ready_at"] = int(time.time())
        print("tinyjev ready in %.1fs (model=%s repo=%s device=%s quantize=%s nr=%s)"
              % (time.time() - t0, TINYJEV_MODEL, repo, TINYJEV_DEVICE,
                 TINYJEV_QUANTIZE or "fp16", "noul" if NR_ON else "off"), flush=True)
    except Exception as exc:  # noqa: BLE001
        _ready.clear()
        _set_phase("error", detail="", error="%s: %s" % (type(exc).__name__, exc),
                   trace=traceback.format_exc(limit=4))
        print("tinyjev lifecycle failed: %r" % exc, flush=True)
    finally:
        with _lifecycle_lock:
            _worker_alive = False


def kick_load(force=False):
    """POST /load: start (or retry) the download+load on a background thread."""
    if _ready.is_set() and not force:
        return {"ok": True, "phase": lifecycle()["phase"], "already": True}
    with _lifecycle_lock:
        busy = _worker_alive
    if busy:
        return {"ok": True, "phase": lifecycle()["phase"], "already": True}
    # _ensure's own _worker_alive guard makes a double start impossible
    threading.Thread(target=_ensure, kwargs={"force": force}, daemon=True).start()
    return {"ok": True, "phase": lifecycle()["phase"], "already": False}


# ------------------------------------------------------------------ classify

def classify(state, categories):
    """One forward pass: Choice over the categories (+ Noul needs_reply when on)."""
    cats = [c for c in (categories or CATEGORIES) if isinstance(c, str) and c.strip()]
    cats = cats or list(CATEGORIES)
    criteria = {c: DESC.get(c, "") for c in cats}
    questions = {"category": {
        "type": "choice",
        "instructions": "Which category does this email belong to?",
        "criteria": criteria,
    }}
    if NR_ON:
        questions["needs_reply"] = {"type": "noul", "instructions": NOUL_INSTRUCTIONS}
    t0 = time.perf_counter()
    with _predict_lock:
        out = _agent.predict({"state": state, "questions": questions})
        tiny_ms = out["execution"]["model_ms"]
        ans = out["states"][0]["answers"]
        cat_ans = ans["category"]
        with _state_lock:
            _lifecycle["calls"] += 1
        result = {
            "model": TINYJEV_MODEL,
            "category": cat_ans["choice"],
            "category_confidence": float(cat_ans["confidence"]),
            "category_probabilities": cat_ans["probabilities"],
            "components": {"tinyjev_ms": round(tiny_ms, 1),
                           "tinyjev_model": TINYJEV_MODEL,
                           "nr_mode": "noul" if NR_ON else "off"},
        }
        if NR_ON:
            p_true = float(ans["needs_reply"]["p_true"])
            nr, conf = noul_decision(p_true)
            result["needs_reply"] = nr
            result["needs_reply_confidence"] = conf
            result["components"]["tinyjev_nr_p_true"] = round(p_true, 3)
        result["error"] = None
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result


# ---------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/healthz", "/health"):
            if _ready.is_set():
                self._send(200, {"ok": True, "model": TINYJEV_MODEL,
                                 "phase": "ready", "nr": lifecycle()["nr"]})
            else:
                self._send(503, {"ok": False, "phase": lifecycle()["phase"],
                                 "progress": lifecycle()["progress"]})
        elif path == "/status":
            self._send(200, lifecycle())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/classify":
            self._classify_route()
        elif path == "/load":
            res = kick_load()
            self._send(200 if res.get("already") else 202, res)
        else:
            self._send(404, {"error": "not found"})

    def _classify_route(self):
        if not _ready.is_set():
            lc = lifecycle()
            self._send(503, {"error": "tinyjev not ready (phase %s)" % lc["phase"],
                             "phase": lc["phase"], "progress": lc["progress"]})
            return
        try:
            body = self._body()
        except Exception:  # noqa: BLE001
            self._send(400, {"error": "invalid JSON body"})
            return
        state = str(body.get("state") or "")
        if not state.strip():
            self._send(400, {"error": "state is required"})
            return
        try:
            self._send(200, classify(state[:20000], body.get("categories")))
        except Exception:  # noqa: BLE001
            self._send(500, {"error": traceback.format_exc(limit=3)})

    def log_message(self, fmt, *args):
        pass


def main():
    print("tinyjev sidecar starting (model=%s device=%s nr=%s)..."
          % (TINYJEV_MODEL, TINYJEV_DEVICE, "noul" if NR_ON else "off"), flush=True)
    threading.Thread(target=_ensure, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

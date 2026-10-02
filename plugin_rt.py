"""Plugin runtime - the supervisor that executes plugin bundles safely.

Design (docs/plugin-architecture.md section 4, adapted at build time):

- One persistent WORKER PROCESS per plugin (plugin_worker.py). The worker hosts
  the QuickJS interpreter; the parent process runs every host call.
- MEMORY is enforced inside the worker (interpreter memory cap from the
  manifest limits).
- WALL CLOCK is enforced here: the parent kills the worker at the deadline.
  This had to move out of the interpreter because the python-quickjs binding
  refuses host callbacks while its time-limit watchdog is active; a process
  kill is also strictly harder to bypass than an interrupt.
- CAPABILITIES: the worker has no I/O whatsoever; the only exit is the host
  bridge, and every host call is mediated + audited here (grants from the
  registry, quotas, allowlists). Ungranted capabilities fail with `denied`.
- Consecutive runtime failures (timeout, crash, interpreter error) auto-disable
  the plugin after STRIKES; success resets the counter.

Test hook: the mock suite drives the real runtime (workers spawn with
sys.executable); `available()` reports whether quickjs is importable.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse

import requests

import config
import plugins as kernel
import store

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_PATH = os.path.join(PROJECT_DIR, "plugin_worker.py")
DEFAULT_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
GRACE_MS = 500
STRIKES = 3
TIMEOUT_SENTINEL = "~mt-timeout~"
MAX_RESULT_BYTES = 65536
MAX_CARD_BYTES = 16384

# host call -> required grant (None = always allowed for an enabled plugin)
HOST_GRANT = {
    "mail.search": "mailbox.read",
    "mail.read": "mailbox.read",
    "llm.complete": "llm.complete",
    "llm.embed": "llm.embed",
    "http.fetch": "net.http",
}

# Composed-UI controller execution runs with a read-only host capability subset:
# config/plugin info/log, mailbox read/search, kv.get/list. Everything that can
# mutate or leave the box is denied for UI-originated calls (net/llm/propose/kv writes).
UI_HOST_ALLOW = {"plugin.info", "log", "config.get", "mail.search", "mail.read",
                 "kv.get", "kv.list"}


class _HostError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _LoadError(Exception):
    pass


def _quickjs_importable():
    try:
        import quickjs  # noqa: F401
        return True
    except Exception:
        return False


def _err_json(code, message):
    return json.dumps({"__error": {"code": code, "message": message}}, ensure_ascii=False)


# ------------------------------------------------------------------ kv helpers

def _kv_usage(conn, plugin_id):
    row = conn.execute("SELECT COALESCE(SUM(LENGTH(v)),0) AS n FROM plugin_kv WHERE plugin_id=?",
                       (plugin_id,)).fetchone()
    return int(row["n"] or 0)


def _kv_get(plugin_id, key):
    if not key:
        return None
    with store.db() as conn:
        row = conn.execute("SELECT v FROM plugin_kv WHERE plugin_id=? AND k=?",
                           (plugin_id, key)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return None


def _kv_set(plugin_id, key, value, quota=None):
    if not key or len(key) > 200:
        raise _HostError("invalid_args", "kv key must be 1-200 chars")
    blob = json.dumps(value, ensure_ascii=False)
    with store.db() as conn:
        if quota is not None:
            used = _kv_usage(conn, plugin_id)
            old = conn.execute("SELECT LENGTH(v) AS n FROM plugin_kv WHERE plugin_id=? AND k=?",
                               (plugin_id, key)).fetchone()
            old_n = int(old["n"] or 0) if old else 0
            if used - old_n + len(blob) > quota:
                raise _HostError("quota", "kv quota exceeded (%d bytes allowed)" % quota)
        conn.execute("INSERT INTO plugin_kv (plugin_id, k, v, updated_ts) VALUES (?,?,?,?) "
                     "ON CONFLICT(plugin_id, k) DO UPDATE SET v=excluded.v, updated_ts=excluded.updated_ts",
                     (plugin_id, key, blob, int(time.time())))
    return True


def _kv_delete(plugin_id, key):
    with store.db() as conn:
        conn.execute("DELETE FROM plugin_kv WHERE plugin_id=? AND k=?", (plugin_id, key))
    return True


def _kv_list(plugin_id, prefix):
    with store.db() as conn:
        rows = conn.execute("SELECT k FROM plugin_kv WHERE plugin_id=? AND k LIKE ? ORDER BY k",
                            (plugin_id, (prefix or "") + "%")).fetchall()
    return [r["k"] for r in rows]


# ------------------------------------------------------------------ host calls

def _mail_search(payload):
    try:
        limit = max(1, min(int(payload.get("limit") or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    q = str(payload.get("query") or "").strip()
    clauses, params = [], []
    if q:
        clauses.append("(from_addr LIKE ? OR subject LIKE ? OR snippet LIKE ?)")
        params += ["%" + q + "%"] * 3
    for key, col in (("sender", "from_addr"), ("subject", "subject")):
        v = str(payload.get(key) or "").strip()
        if v:
            clauses.append("%s LIKE ?" % col)
            params.append("%" + v + "%")
    folder = str(payload.get("folder") or "").strip()
    if folder:
        clauses.append("folder = ?")
        params.append(folder)
    since_days = payload.get("since_days")
    if since_days:
        try:
            cutoff = int(time.time()) - int(float(since_days)) * 86400
            clauses.append("COALESCE(NULLIF(date_ts,0), processed_at, 0) >= ?")
            params.append(cutoff)
        except (TypeError, ValueError):
            pass
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with store.db() as conn:
        rows = conn.execute("SELECT id, folder, uid, subject, from_addr, date, snippet, "
                            "llm_category, llm_needs_reply "
                            "FROM messages" + where + " ORDER BY id DESC LIMIT ?",
                            params + [limit]).fetchall()
    return [{"id": r["id"], "folder": r["folder"], "uid": r["uid"], "subject": r["subject"],
             "from": r["from_addr"], "date": r["date"],
             "snippet": (r["snippet"] or "")[:160],
             "category": r["llm_category"], "needs_reply": bool(r["llm_needs_reply"])}
            for r in rows]


def _mail_read(payload):
    try:
        mid = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        mid = 0
    with store.db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if row is None:
        raise _HostError("invalid_args", "message %s is not in the local index" % mid)
    d = dict(row)
    return {"id": d.get("id"), "folder": d.get("folder"), "uid": d.get("uid"),
            "subject": d.get("subject"), "from": d.get("from_addr"), "to": d.get("to_addr"),
            "date": d.get("date"), "body_text": (d.get("snippet") or "")[:4000],
            "category": d.get("llm_category"),
            "tags": [d["user_tag"]] if d.get("user_tag") else [],
            "needs_reply": bool(d.get("llm_needs_reply"))}


def _llm_complete(payload):
    from engine import LLMClient  # lazy: plugin_rt is imported by engine call sites
    prompt = str(payload.get("prompt") or "")[:20000]
    system = str(payload.get("system") or
                 "You are a helpful assistant inside a local email app. Answer concisely.")[:4000]
    try:
        max_tokens = max(16, min(int(payload.get("max_tokens") or 512), 2048))
    except (TypeError, ValueError):
        max_tokens = 512
    if not prompt.strip():
        raise _HostError("invalid_args", "prompt is required")
    text = LLMClient()._chat(system, prompt, json_mode=bool(payload.get("json")),
                             max_tokens=max_tokens)
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return {"text": text}


def _llm_embed(payload):
    from rag import embed
    texts = payload.get("texts")
    if not isinstance(texts, list) or not texts:
        raise _HostError("invalid_args", "texts must be a non-empty array")
    texts = [str(t)[:8000] for t in texts][:32]
    return embed(texts, kind="document")


def _http_fetch(plugin_id, row, payload):
    url = str(payload.get("url") or "")
    init = payload.get("init") or {}
    if not isinstance(init, dict):
        init = {}
    m = urllib.parse.urlparse(url)
    if m.scheme not in ("http", "https") or not m.hostname:
        raise _HostError("invalid_args", "url must be http(s)")
    host = m.hostname.lower()
    allowed = ((row["manifest"].get("net") or {}).get("hosts") or [])
    ok = False
    for pat in allowed:
        p = str(pat).lower()
        if p.startswith("*."):
            if host.endswith(p[1:]) and host != p[2:]:
                ok = True
                break
        elif host == p.split(":")[0]:
            ok = True
            break
    if not ok and ((row["manifest"].get("net") or {}).get("allow_config_hosts")):
        # user-typed endpoints (e.g. a webhook URL) extend the allowlist; the
        # config value itself is the consent, and every call is still audited
        for pat in (kernel.get_config(plugin_id).get("allowed_hosts") or []):
            p = str(pat).lower()
            if p.startswith("*."):
                if host.endswith(p[1:]) and host != p[2:]:
                    ok = True
                    break
            elif host == p.split(":")[0]:
                ok = True
                break
    if not ok:
        raise _HostError("denied", "host '%s' is not allowlisted for this plugin" % host)
    cap = int((row["manifest"].get("net") or {}).get("max_requests_per_day") or 50)
    today = time.strftime("%Y-%m-%d")
    day = _kv_get(plugin_id, "__http_day") or {}
    n = int(day.get("n") or 0) if day.get("d") == today else 0
    if cap and n >= cap:
        raise _HostError("quota", "daily http request limit reached (%d)" % cap)
    timeout = min(max(float(init.get("timeout_ms") or 15000) / 1000.0, 1.0), 30.0)
    method = str(init.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        raise _HostError("invalid_args", "method must be GET or POST")
    headers = {str(k): str(v) for k, v in (init.get("headers") or {}).items()}
    r = requests.request(method, url, headers=headers, data=init.get("body"), timeout=timeout)
    _kv_set(plugin_id, "__http_day", {"d": today, "n": n + 1})
    return {"status": r.status_code,
            "headers": {k: str(v)[:200] for k, v in list(r.headers.items())[:20]},
            "body": (r.text or "")[:262144]}


def _action_propose(plugin_id, row, payload):
    card = payload.get("card")
    if not isinstance(card, dict) or not str(card.get("title") or "").strip():
        raise _HostError("invalid_args", "card.title is required")
    name = row["manifest"].get("name") or plugin_id
    aid = store.add_agent_action("plugin:" + plugin_id, "plugin_proposal",
                                 "Plugin '%s': %s" % (name, str(card.get("title"))[:120]),
                                 {"plugin": plugin_id, "card": card}, 0)
    return {"action_id": aid}


# ------------------------------------------------------------------ worker

class _Worker:
    def __init__(self, plugin_id):
        self.plugin_id = plugin_id
        self.proc = None
        self.q = queue.Queue()
        self.th = None
        self.key = None
        self._seq = 0

    def spawn(self):
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(
            [sys.executable, WORKER_PATH], cwd=PROJECT_DIR,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self.th = threading.Thread(target=self._pump, daemon=True)
        self.th.start()

    def _pump(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.q.put(line)
        except Exception:
            pass
        finally:
            self.q.put(None)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def next_seq(self):
        self._seq += 1
        return self._seq

    def read(self, timeout):
        """-> line str | None (EOF) | TIMEOUT_SENTINEL (nothing within timeout)."""
        try:
            return self.q.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            return TIMEOUT_SENTINEL

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.stdin.close()
            except Exception:
                pass


class PluginRuntime:
    def __init__(self):
        self._workers = {}
        self._locks = {}
        self._strikes = {}
        self._tls = threading.local()
        self._guard = threading.Lock()

    # ---- lifecycle

    def available(self):
        return os.path.isfile(WORKER_PATH) and _quickjs_importable()

    def invalidate(self, plugin_id):
        with self._guard:
            w = self._workers.pop(plugin_id, None)
        if w:
            w.kill()

    def shutdown(self):
        with self._guard:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.kill()

    def _lock(self, plugin_id):
        with self._guard:
            return self._locks.setdefault(plugin_id, threading.Lock())

    # ---- worker protocol

    _DEADLINE = "~mt-deadline~"
    _EOF = "~mt-eof~"

    def _read_until(self, plugin_id, w, deadline):
        """Read worker lines until a non-host message, answering host calls
        inline (boot itself calls plugin.info / log through the bridge).
        Returns ("msg", dict) | (_DEADLINE, None) | (_EOF, None)."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return self._DEADLINE, None
            line = w.read(min(remaining, 5.0))
            if line == TIMEOUT_SENTINEL:
                continue
            if line is None:
                return self._EOF, None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "host" in msg:
                out = self._host(plugin_id, str(msg.get("host") or ""),
                                 msg.get("payload") or "{}")
                try:
                    w.send({"host_result": {"rid": msg.get("rid"), "out": out}})
                except Exception:
                    return self._EOF, None
                continue
            return "msg", msg

    def _call_worker(self, plugin_id, row, cmd, profile=None):
        # Capability profile is per-call and thread-local: the host bridge pumps
        # on THIS thread inside _call_worker_inner, so a concurrent call for the
        # same plugin (matcher/schedule/tool) can never overwrite it (AR2-1).
        prev = getattr(self._tls, "profile", None)
        self._tls.profile = profile
        try:
            return self._call_worker_inner(plugin_id, row, cmd)
        finally:
            self._tls.profile = prev

    def _call_worker_inner(self, plugin_id, row, cmd):
        """Send one command to the plugin worker and pump until its reply.

        Returns (status, payload): status in ok | load | spawn | write |
        timeout | died; payload = worker message (ok) | error text | timeout_ms.
        """
        limits = row["manifest"].get("limits") or {}
        tmo = min(int(limits.get("timeout_ms") or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)
        lock = self._lock(plugin_id)
        with lock:
            try:
                w = self._worker_for(row)
            except _LoadError as exc:
                store.log_event("warn", "plugin '%s' load failed: %s" % (plugin_id, exc))
                kernel.set_last_error(plugin_id, "load failed: %s" % str(exc)[:200])
                return "load", str(exc)[:200]
            except Exception as exc:
                return "spawn", "runtime spawn failed: %r" % exc
            seq = w.next_seq()
            t0 = time.time()
            deadline = t0 + (tmo + GRACE_MS) / 1000.0
            out_cmd = dict(cmd)
            out_cmd["seq"] = seq
            out_cmd["deadline_ms"] = tmo
            try:
                w.send(out_cmd)
            except Exception as exc:
                self.invalidate(plugin_id)
                return "write", "worker write failed: %r" % exc
            while True:
                kind, msg = self._read_until(plugin_id, w, deadline)
                if kind == self._DEADLINE:
                    self.invalidate(plugin_id)
                    return "timeout", tmo
                if kind == self._EOF:
                    self.invalidate(plugin_id)
                    return "died", "plugin worker died unexpectedly"
                if msg.get("seq") == seq:
                    msg["_ms"] = int((time.time() - t0) * 1000)
                    return "ok", msg

    def matcher(self, plugin_id, payload):
        """Rule/flow condition evaluation (hot path). Returns True/False."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled"):
            return False
        if "matcher" not in (row["manifest"].get("kind") or []):
            return False
        if not self.available():
            return False
        status, res = self._call_worker(plugin_id, row, {"cmd": "matcher", "input": payload})
        if status != "ok":
            if status == "timeout":
                self._strike(plugin_id, "timeout",
                             "matcher timed out after %dms (worker killed)" % res)
            elif status != "load":
                self._strike(plugin_id, "internal", "matcher failed: %s" % str(res)[:160])
            return False
        if "error" in res:
            e = res.get("error") or {}
            self._strike(plugin_id, str(e.get("code") or "internal"),
                         "matcher error: %s" % str(e.get("message") or "")[:200])
            return False
        out = res.get("result")
        if isinstance(out, dict):
            return bool(out.get("match"))
        return bool(out)

    def draft(self, plugin_id, payload):
        """Draft-provider: the reply body a flow step would draft. None on failure."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled") \
                or "draft-provider" not in (row["manifest"].get("kind") or []):
            return None
        if not self.available():
            return None
        status, res = self._call_worker(plugin_id, row, {"cmd": "draft", "input": payload})
        if status != "ok":
            if status == "timeout":
                self._strike(plugin_id, "timeout",
                             "draft provider timed out after %dms (worker killed)" % res)
            elif status != "load":
                self._strike(plugin_id, "internal", "draft provider failed: %s" % str(res)[:160])
            return None
        if "error" in res:
            e = res.get("error") or {}
            self._strike(plugin_id, str(e.get("code") or "internal"),
                         "draft provider error: %s" % str(e.get("message") or "")[:200])
            return None
        out = res.get("result")
        if isinstance(out, str):
            out = {"text": out}
        if not isinstance(out, dict):
            return None
        return {"text": str(out.get("text") or "")[:20000]}

    def retriever(self, plugin_id, payload):
        """Search re-ranker: {"ids": [...]} in the plugin's preferred order, or None."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled") \
                or "retriever" not in (row["manifest"].get("kind") or []):
            return None
        if not self.available():
            return None
        status, res = self._call_worker(plugin_id, row, {"cmd": "rank", "input": payload})
        if status != "ok":
            if status == "timeout":
                self._strike(plugin_id, "timeout",
                             "retriever timed out after %dms (worker killed)" % res)
            elif status != "load":
                self._strike(plugin_id, "internal", "retriever failed: %s" % str(res)[:160])
            return None
        if "error" in res:
            e = res.get("error") or {}
            self._strike(plugin_id, str(e.get("code") or "internal"),
                         "retriever error: %s" % str(e.get("message") or "")[:200])
            return None
        out = res.get("result")
        if isinstance(out, dict):
            out = out.get("ids")
        if not isinstance(out, list):
            return None
        ids = []
        for x in out[:200]:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        return {"ids": ids} if ids else None

    def call_event(self, plugin_id, event):
        """One-way integration event delivery (dispatcher thread context)."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled") \
                or "integration" not in (row["manifest"].get("kind") or []):
            return False
        if not self.available():
            return False
        status, res = self._call_worker(plugin_id, row, {"cmd": "event", "input": event})
        if status == "ok" and "error" not in res:
            return True
        if status == "timeout":
            self._strike(plugin_id, "timeout", "event handler timed out (%dms)" % res)
        elif status not in ("load",):
            self._strike(plugin_id, "internal", "event handler failed: %s" % str(res)[:160])
        return False

    def schedule(self, plugin_id, payload):
        """Run a plugin's scheduled entrypoint. Returns the ToolResult dict or None."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled") or not row["manifest"].get("schedule"):
            return None
        if not self.available():
            return None
        status, res = self._call_worker(plugin_id, row, {"cmd": "schedule", "input": payload})
        if status != "ok":
            if status == "timeout":
                self._strike(plugin_id, "timeout", "scheduled run timed out (%dms)" % res)
            elif status != "load":
                self._strike(plugin_id, "internal", "scheduled run failed: %s" % str(res)[:160])
            return None
        if "error" in res:
            e = res.get("error") or {}
            self._strike(plugin_id, str(e.get("code") or "internal"),
                         "scheduled run error: %s" % str(e.get("message") or "")[:200])
            return None
        out = res.get("result")
        return out if isinstance(out, dict) else None

    def classify(self, plugin_id, payload):
        """Run a classifier-kind plugin (mirrors a native heuristic's model).

        Returns {"label", "confidence", "detail"} or None (abstain/failure).
        Timeouts and interpreter errors count as strikes like tool calls do.
        """
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled"):
            return None
        if "classifier" not in (row["manifest"].get("kind") or []):
            return None
        if not self.available():
            return None
        status, res = self._call_worker(plugin_id, row, {"cmd": "classify", "input": payload})
        if status == "ok":
            if "error" in res:
                e = res.get("error") or {}
                self._strike(plugin_id, str(e.get("code") or "internal"),
                             "classifier error: %s" % str(e.get("message") or "")[:200])
                return None
            out = res.get("result")
            if not isinstance(out, dict):
                return None
            try:
                conf = float(out.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            return {"label": str(out.get("label") or ""), "confidence": conf,
                    "detail": str(out.get("detail") or "")}
        if status == "load":
            return None  # recorded in last_error by _call_worker
        if status == "timeout":
            self._strike(plugin_id, "timeout",
                         "classifier timed out after %dms (worker killed)" % res)
        else:
            self._strike(plugin_id, "internal", "classifier call failed: %s" % str(res)[:160])
        return None

    def _worker_for(self, row):
        pid = row["id"]
        key = (row["dir"], (row["manifest"].get("entrypoint") or ""), row["entry_sha256"],
               row["manifest_sha256"])
        w = self._workers.get(pid)
        if w and w.alive() and w.key == key:
            return w
        if w:
            w.kill()
            self._workers.pop(pid, None)
        w = _Worker(pid)
        w.spawn()
        first = w.read(15.0)
        ready = None
        if first and first != TIMEOUT_SENTINEL:
            try:
                ready = json.loads(first)
            except ValueError:
                ready = None
        if not ready or not ready.get("ready"):
            w.kill()
            raise _LoadError("worker failed to start (%s)" % str(ready or first)[:160])
        limits = row["manifest"].get("limits") or {}
        w.send({"cmd": "load", "dir": row["dir"],
                "entry": row["manifest"].get("entrypoint") or "",
                "memory_mb": int(limits.get("memory_mb") or 64),
                "plugin_id": pid, "version": row["version"]})
        kind, resp = self._read_until(pid, w, time.time() + 20.0)
        if kind != "msg" or not isinstance(resp, dict) or not resp.get("loaded"):
            w.kill()
            err = ((resp or {}).get("error") or {}) if isinstance(resp, dict) else {}
            if err.get("message"):
                message = str(err["message"])[:200]
            elif kind == self._DEADLINE:
                message = "worker timed out during load"
            else:
                message = "did not load"
            raise _LoadError(message)
        w.key = key
        self._workers[pid] = w
        return w

    # ---- host calls (parent side of the bridge)

    def _host(self, plugin_id, name, payload_json):
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled"):
            return _err_json("denied", "plugin '%s' is not enabled" % plugin_id)
        if getattr(self._tls, "profile", None) == "ui" and name not in UI_HOST_ALLOW:
            return _err_json("denied",
                             "capability '%s' is not available to the composed UI" % name)
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        need = HOST_GRANT.get(name)
        if need and need not in (row.get("grants") or []):
            return _err_json("denied", "plugin '%s' is not granted '%s'" % (plugin_id, need))
        try:
            if name == "plugin.info":
                out = {"id": plugin_id, "version": row["version"]}
            elif name == "log":
                level = str(payload.get("level") or "info")
                message = str(payload.get("message") or "")[:500]
                if level in ("warn", "error"):
                    store.log_event("warn", "plugin '%s': %s" % (plugin_id, message))
                out = True
            elif name == "config.get":
                out = {"defaults": row["manifest"].get("config") or {},
                       "values": store.get_setting("plugin_config:" + plugin_id, {}) or {}}
            elif name == "kv.get":
                out = _kv_get(plugin_id, str(payload.get("key") or ""))
            elif name == "kv.set":
                quota = int((row["manifest"].get("limits") or {}).get("kv_bytes") or 65536)
                out = _kv_set(plugin_id, str(payload.get("key") or ""), payload.get("value"), quota)
            elif name == "kv.delete":
                out = _kv_delete(plugin_id, str(payload.get("key") or ""))
            elif name == "kv.list":
                out = _kv_list(plugin_id, str(payload.get("prefix") or ""))
            elif name == "mail.search":
                out = _mail_search(payload)
            elif name == "mail.read":
                out = _mail_read(payload)
            elif name == "llm.complete":
                out = _llm_complete(payload)
            elif name == "llm.embed":
                out = _llm_embed(payload)
            elif name == "http.fetch":
                out = _http_fetch(plugin_id, row, payload)
            elif name == "action.propose":
                out = _action_propose(plugin_id, row, payload)
            else:
                raise _HostError("internal", "unknown host call '%s'" % name)
        except _HostError as exc:
            return _err_json(exc.code, str(exc))
        except Exception as exc:
            return _err_json("internal", repr(exc)[:250])
        return json.dumps(out, ensure_ascii=False)

    # ---- invoke

    def _strike(self, plugin_id, code, message):
        n = self._strikes.get(plugin_id, 0) + 1
        self._strikes[plugin_id] = n
        store.log_event("warn", "plugin '%s' runtime failure %d/%d: %s"
                        % (plugin_id, n, STRIKES, message[:160]))
        if n >= STRIKES:
            self._strikes.pop(plugin_id, None)
            kernel.disable_with_error(
                plugin_id, "auto-disabled after %d consecutive failures: %s" % (n, message[:160]))
            self.invalidate(plugin_id)
        return {"ok": False, "summary": message,
                "result": {"error": {"code": code, "message": message}}}

    def _finish(self, plugin_id, tool, msg, elapsed_ms):
        if "error" in msg:
            e = msg.get("error") or {}
            code = str(e.get("code") or "internal")
            message = str(e.get("message") or "plugin error")
            store.log_event("warn", "plugin '%s' %s failed (%dms): %s"
                            % (plugin_id, tool, elapsed_ms, message[:160]))
            return self._strike(plugin_id, code, "plugin error: %s" % message[:240])
        raw = msg.get("result") or {}
        ok = bool(raw.get("ok"))
        summary = str(raw.get("summary") or tool)[:500]
        data = raw.get("data")
        out = {"ok": ok, "summary": summary}
        if data is None:
            data = {}
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob) > MAX_RESULT_BYTES:
            data = {"truncated": True, "note": "result data exceeded 64 KB and was dropped"}
        out["result"] = data if isinstance(data, dict) else {"data": data}
        card = raw.get("card")
        if isinstance(card, dict) and len(json.dumps(card, ensure_ascii=False)) <= MAX_CARD_BYTES:
            out["card"] = card
        if not ok and isinstance(raw.get("error"), dict):
            out["result"]["error"] = raw["error"]
        if ok:
            self._strikes.pop(plugin_id, None)
        store.log_event("plugin", "%s.%s %s in %dms"
                        % (plugin_id, tool, "ok" if ok else "returned an error", elapsed_ms))
        return out

    def invoke(self, plugin_id, tool, args, session_id=0):
        row = kernel.get(plugin_id)
        if not row:
            return {"ok": False, "summary": "unknown plugin '%s'" % plugin_id,
                    "result": {"error": {"code": "internal", "message": "unknown plugin"}}}
        if not row.get("enabled"):
            return {"ok": False, "summary": "plugin '%s' is not enabled" % plugin_id,
                    "result": {"error": {"code": "denied",
                                         "message": "plugin is disabled; enable it in the Plugins page"}}}
        tdef = None
        for t in (row["manifest"].get("tools") or []):
            if t.get("name") == tool:
                tdef = t
                break
        if tdef is None:
            return {"ok": False, "summary": "plugin '%s' has no tool '%s'" % (plugin_id, tool),
                    "result": {"error": {"code": "internal",
                                         "message": "tool not declared in the manifest"}}}
        verr = _validate_args(tdef.get("parameters") or {}, args or {})
        if verr:
            return {"ok": False, "summary": verr,
                    "result": {"error": {"code": "invalid_args", "message": verr}}}
        if not self.available():
            return {"ok": False, "summary": "plugin runtime unavailable (quickjs not installed)",
                    "result": {"error": {"code": "internal", "message": "runtime unavailable"}}}
        status, payload = self._call_worker(plugin_id, row,
                                            {"cmd": "invoke", "tool": tool, "args": args or {}})
        if status == "ok":
            return self._finish(plugin_id, tool, payload, payload.get("_ms") or 0)
        if status == "load":
            return {"ok": False, "summary": "plugin failed to load: %s" % payload,
                    "result": {"error": {"code": "internal", "message": payload}}}
        if status == "timeout":
            return self._strike(plugin_id, "timeout",
                                "plugin timed out after %dms (worker killed)" % payload)
        return self._strike(plugin_id, "internal", str(payload))

    def invoke_ui(self, plugin_id, page, op, args):
        """Route one trusted browser-view operation through the sandbox path.

        The caller (app.py) has already checked the view session, approval and
        rate limits; here we re-check enable/mode/page/allowlist, then run the
        tool through invoke() so grants/limits/audit apply unchanged."""
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled"):
            return {"ok": False, "error": {"code": "disabled",
                                           "message": "plugin is disabled"}}
        if kernel.ui_mode(row) != "trusted":
            return {"ok": False, "error": {"code": "forbidden",
                                           "message": "plugin has no trusted browser view"}}
        if not kernel.ui_page_declared(row, page):
            return {"ok": False, "error": {"code": "not_found",
                                           "message": "unknown page"}}
        if op not in kernel.ui_operations(row):
            return {"ok": False, "error": {"code": "forbidden",
                                           "message": "operation not declared for the browser UI"}}
        res = self.invoke(plugin_id, op, args or {})
        if res.get("ok"):
            data = res.get("result")
            data = data if isinstance(data, dict) else {"data": data}
            return {"ok": True, "data": data, "summary": res.get("summary") or ""}
        err = {}
        if isinstance(res.get("result"), dict):
            err = res["result"].get("error") or {}
        code = err.get("code") or "internal"
        message = err.get("message") or res.get("summary") or "call failed"
        if code == "internal" and "denied" in str(message).lower():
            code = "denied"
        return {"ok": False, "error": {"code": code, "message": message}}

    # ---- composed controller execution (read-only host capability subset)

    def _ui_call(self, plugin_id, cmd, payload):
        row = kernel.get(plugin_id)
        if not row or not row.get("enabled"):
            return {"error": {"code": "disabled", "message": "plugin is disabled"}}
        if kernel.ui_mode(row) != "composed" and cmd != "ui_close":
            return {"error": {"code": "forbidden",
                              "message": "plugin has no composed UI controller"}}
        if not self.available():
            return {"error": {"code": "internal", "message": "runtime unavailable"}}
        status, res = self._call_worker(plugin_id, row,
                                        {"cmd": cmd, "input": payload or {}}, profile="ui")
        if status != "ok":
            if status == "timeout":
                return {"error": {"code": "timeout",
                                  "message": "controller timed out after %s ms" % res}}
            return {"error": {"code": "internal", "message": str(res)[:200]}}
        if isinstance(res, dict) and "error" in res:
            e = res.get("error") or {}
            return {"error": {"code": str(e.get("code") or "internal"),
                              "message": str(e.get("message") or "")[:300]}}
        out = res.get("result") if isinstance(res, dict) else None
        return {"result": out if isinstance(out, dict) else None}

    def ui_open(self, plugin_id, initial_state):
        return self._ui_call(plugin_id, "ui_open", {"state": initial_state or {}})

    def ui_dispatch(self, plugin_id, state, event):
        return self._ui_call(plugin_id, "ui_dispatch",
                             {"state": state or {}, "event": event or {}})

    def ui_close(self, plugin_id, state):
        return self._ui_call(plugin_id, "ui_close", {"state": state or {}})


def _validate_args(params, args):
    """Light JSON-Schema check: required present, declared scalar types correct."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = params.get("properties") or {}
    for r in params.get("required") or []:
        if r not in args or args[r] in (None, ""):
            return "missing required argument '%s'" % r
    types = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "array": list, "object": dict}
    for k, v in args.items():
        t = (props.get(k) or {}).get("type")
        if not t:
            continue
        if t in ("integer", "number") and isinstance(v, bool):
            return "argument '%s' must be %s" % (k, t)
        if t in types and not isinstance(v, types[t]):
            return "argument '%s' must be %s" % (k, t)
    return None


runtime = PluginRuntime()


# ---------------------------------------------------------------- events

_event_queue = queue.Queue(maxsize=200)
_event_thread = None
_event_lock = threading.Lock()
_event_target_cache = {"ts": 0.0, "ids": []}


def _integration_targets():
    now = time.time()
    age = now - _event_target_cache["ts"]
    if _event_target_cache["ids"] and age < 10:
        return _event_target_cache["ids"]
    if not _event_target_cache["ids"] and age < 1:
        return _event_target_cache["ids"]
    try:
        ids = [r["id"] for r in kernel.enabled_of_kind("integration")]
    except Exception:
        ids = []
    _event_target_cache["ts"] = now
    _event_target_cache["ids"] = ids
    return ids


def event_cache_reset():
    """Force the next emit to re-resolve targets (called on enable/disable)."""
    _event_target_cache["ts"] = 0.0
    _event_target_cache["ids"] = []


def emit_event(event_type, payload):
    """Queue one kernel event for enabled integration plugins (non-blocking).

    Dispatched on a background thread so the mail pipeline never waits on a
    plugin's network call. Returns the number of addressed plugins."""
    targets = _integration_targets()
    if not targets:
        return 0
    _ensure_dispatcher()
    try:
        _event_queue.put_nowait({"type": str(event_type), "payload": payload or {},
                                 "targets": targets, "ts": int(time.time())})
    except queue.Full:
        try:
            store.log_event("warn", "plugin events: queue full; dropped %r" % event_type)
        except Exception:
            pass
    return len(targets)


def _ensure_dispatcher():
    global _event_thread
    with _event_lock:
        if _event_thread is not None and _event_thread.is_alive():
            return
        _event_thread = threading.Thread(target=_event_loop, daemon=True,
                                         name="plugin-events")
        _event_thread.start()


def _event_loop():
    while True:
        item = _event_queue.get()
        if item is None:
            return
        for pid in item["targets"]:
            try:
                runtime.call_event(pid, {"type": item["type"], "payload": item["payload"],
                                         "ts": item["ts"]})
            except Exception:
                pass


def available():
    return runtime.available()


def invoke_tool(full_name, args, session_id=0):
    """Route a `plugin__<id>__<tool>` name to the runtime; None if not ours."""
    sp = kernel.split_tool_name(full_name)
    if not sp:
        return None
    return runtime.invoke(sp[0], sp[1], args or {}, session_id)


def classify(plugin_id, payload):
    """Classifier-kind plugin call (used by the heuristics fallback)."""
    return runtime.classify(plugin_id, payload)


def matcher(plugin_id, payload):
    """Rule/flow condition call (used by engine._cond_field)."""
    return runtime.matcher(plugin_id, payload)


def draft(plugin_id, payload):
    """Draft-provider call (flow draft steps + simulator preview)."""
    return runtime.draft(plugin_id, payload)


def retriever(plugin_id, payload):
    """Search re-ranker call (assistant semantic_search)."""
    return runtime.retriever(plugin_id, payload)


# ---------------------------------------------------------------- scheduling

def run_due_schedules(now=None, force=False):
    """Invoke enabled plugins whose manifest declares `schedule` and are due.

    Runs on the scheduler thread (never the mail pipeline). Returns a list of
    {plugin, tool, ok, summary}; never raises. `last_run` is persisted in
    plugin_kv before the call so a failing plugin cannot hot-loop.
    """
    now = int(now or time.time())
    results = []
    try:
        if not store.get_setting("plugin_schedules_enabled", 1):
            return results
        rows = [r for r in kernel.list_rows(enabled_only=True)
                if (r["manifest"] or {}).get("schedule")]
    except Exception:
        return results
    for row in rows:
        pid = row["id"]
        try:
            sched = row["manifest"].get("schedule") or {}
            if sched.get("enabled") is False:
                continue
            every = max(15, int(sched.get("every_minutes") or 1440))
            state = _kv_get(pid, "__schedule") or {}
            last = int(state.get("last") or 0)
            if not force and last and (now - last) < every * 60:
                continue
            tools = row["manifest"].get("tools") or []
            tool = str(sched.get("run_tool") or "") or (tools[0]["name"] if tools else "")
            if not tool:
                continue
            _kv_set(pid, "__schedule", {"last": now})
            res = runtime.schedule(pid, {"every_minutes": every, "last_run": last,
                                         "now": now, "run_tool": tool})
            ok = bool(res and res.get("ok"))
            results.append({"plugin": pid, "tool": tool, "ok": ok,
                            "summary": (res or {}).get("summary") or ""})
            store.log_event("plugin", "scheduled %s.%s %s"
                            % (pid, tool, "ok" if ok else "no result"))
        except Exception as exc:
            try:
                store.log_event("warn", "scheduled run for '%s' failed: %r" % (pid, exc))
            except Exception:
                pass
    return results


class PluginScheduler(threading.Thread):
    """Background loop that fires `run_due_schedules` roughly every 30s."""

    def __init__(self):
        super().__init__(daemon=True, name="plugin-scheduler")
        self.stop_flag = threading.Event()

    def run(self):
        self.stop_flag.wait(5)  # let the UI + registry settle at boot
        while not self.stop_flag.is_set():
            try:
                run_due_schedules()
            except Exception:
                pass
            self.stop_flag.wait(30)


scheduler = PluginScheduler()


atexit.register(runtime.shutdown)

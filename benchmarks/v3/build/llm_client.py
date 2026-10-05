"""OpenAI-compatible LLM client, reproducibility cache and fake client (WP6 pilot).

This is the network boundary for the bottom-up LLM rendering path.  Nothing in
the deterministic authoring path imports it: unit tests use :class:`FakeClient`
(recorded outputs, zero model calls) and the bulk pilot uses
:class:`OpenAICompatClient` against a local vLLM server.

Reproducibility contract (FR7): every generation is cached under a key derived
from the declared model id, an optional revision hint, the endpoint, the
canonical prompt hash, the decode seed, temperature, the candidate index and the
retry index.  The stored envelope records all of those plus the raw response, so
a cache entry can be audited without re-running the model.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8046/v1"
DEFAULT_TIMEOUT = 180.0


class LLMError(RuntimeError):
    """Raised when the serving endpoint cannot satisfy a chat request."""


def canonical(obj):
    """Stable JSON encoding used for hashing/keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def prompt_hash(messages, params, model, endpoint):
    """Content hash of everything that influences a completion."""
    return hashlib.sha256(canonical({
        "messages": messages, "params": {k: v for k, v in (params or {}).items()
                                          if k != "candidate_index"},
        "model": model, "endpoint": endpoint,
    }).encode("utf-8")).hexdigest()


class OpenAICompatClient(object):
    """/v1/chat/completions client built on the standard library only."""

    def __init__(self, base_url=DEFAULT_ENDPOINT, model="gemma-4-26b-a4b",
                 api_key="EMPTY", timeout=DEFAULT_TIMEOUT, revision=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = float(timeout)
        # A revision hint is recorded in provenance; it is never sent on the wire.
        self.revision = revision

    def endpoint(self):
        return self.base_url

    def chat(self, messages, *, temperature=0.9, max_tokens=1024, seed=None,
             top_p=0.95, extra=None):
        payload = {"model": self.model, "messages": messages,
                   "temperature": float(temperature), "max_tokens": int(max_tokens),
                   "top_p": float(top_p)}
        if seed is not None:
            payload["seed"] = int(seed)
        if extra:
            payload.update(extra)
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % self.api_key}
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise LLMError("chat request failed: %s" % exc)
        except ValueError as exc:
            raise LLMError("chat response was not JSON: %s" % exc)
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("unexpected chat response shape: %s" % exc)
        return {
            "content": content,
            "finish_reason": choice.get("finish_reason"),
            "usage": body.get("usage") or {},
            "request": payload,
        }


class FakeClient(object):
    """Recorded-output client for offline unit tests (zero network, zero GPU).

    ``responses`` is a list of either dicts like ``{"content": "..."}`` or
    callables ``fn(messages, params) -> dict``.  Calls are recorded so tests can
    assert retry/candidate behaviour.  The cycle is deterministic.
    """

    def __init__(self, responses, model="fake-llm", revision="fake"):
        self.responses = list(responses)
        self.model = model
        self.revision = revision
        self.calls = []
        self._i = 0

    def endpoint(self):
        return "fake://local"

    def chat(self, messages, *, temperature=0.9, max_tokens=1024, seed=None,
             top_p=0.95, extra=None, candidate_index=0, retry=0):
        params = {"temperature": temperature, "max_tokens": max_tokens,
                  "seed": seed, "top_p": top_p}
        self.calls.append({"messages": list(messages), "params": params,
                           "candidate_index": candidate_index, "retry": retry})
        if not self.responses:
            raise LLMError("FakeClient has no recorded responses")
        r = self.responses[self._i % len(self.responses)]
        self._i += 1
        if callable(r):
            r = r(messages, params)
        out = dict(r)
        out.setdefault("finish_reason", "stop")
        out.setdefault("usage", {})
        return out


# --------------------------------------------------------------------- cache

class MessageCache(object):
    """Content-addressed on-disk cache for generated messages.

    The cache lives outside the repository (see ``pilot.py`` default).  A
    disabled cache is a no-op, so unit tests never touch the disk.
    """

    def __init__(self, root=None, enabled=True):
        self.root = root
        self.enabled = bool(enabled and root)

    def key(self, *, endpoint, model, revision, call_prompt_hash, seed,
            temperature, candidate_index, retry, extra_key=None):
        material = {
            "endpoint": endpoint, "model": model, "revision": revision,
            "prompt_hash": call_prompt_hash,
            "seed": None if seed is None else int(seed),
            "temperature": round(float(temperature), 6),
            "candidate_index": int(candidate_index), "retry": int(retry),
            "extra_key": extra_key,
        }
        return hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()

    def _path(self, key):
        return os.path.join(self.root, key[:2], key + ".json")

    def get(self, key):
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def put(self, key, envelope):
        if not self.enabled:
            return
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)


class CachedChatClient(object):
    """Wraps a chat client with the :class:`MessageCache` and provenance.

    ``cache_meta`` names the provenance dimensions the renderer must fold into
    the key (candidate index and retry index are added around each call).
    """

    def __init__(self, client, cache=None, revision_hint=None):
        self.client = client
        self.cache = cache or MessageCache(enabled=False)
        self.revision_hint = revision_hint or getattr(client, "revision", None)
        self.model = getattr(client, "model", "unknown")
        self.endpoint = getattr(client, "endpoint", lambda: "unknown")()
        self.last_provenance = None

    def chat(self, messages, *, seed=None, temperature=0.9, candidate_index=0,
             retry=0, extra_key=None, **kwargs):
        ph = prompt_hash(messages, {"temperature": temperature, "seed": seed,
                                    "candidate_index": candidate_index},
                         self.model, self.endpoint)
        key = self.cache.key(endpoint=self.endpoint, model=self.model,
                             revision=self.revision_hint, call_prompt_hash=ph,
                             seed=seed, temperature=temperature,
                             candidate_index=candidate_index, retry=retry,
                             extra_key=extra_key)
        cached = self.cache.get(key)
        if cached is not None:
            self.last_provenance = {"cache": "hit", "key": key,
                                    "provenance": cached.get("provenance")}
            return cached["response"]
        started = time.time()
        response = self.client.chat(messages, temperature=temperature, seed=seed,
                                    **kwargs)
        elapsed = time.time() - started
        provenance = {
            "endpoint": self.endpoint, "model": self.model,
            "revision_hint": self.revision_hint, "prompt_hash": ph,
            "seed": None if seed is None else int(seed),
            "temperature": round(float(temperature), 6),
            "candidate_index": int(candidate_index), "retry": int(retry),
            "extra_key": extra_key,
        }
        self.cache.put(key, {"provenance": provenance, "response": response,
                             "latency_s": round(elapsed, 3)})
        self.last_provenance = {"cache": "miss", "key": key,
                                "provenance": provenance,
                                "latency_s": round(elapsed, 3)}
        return response

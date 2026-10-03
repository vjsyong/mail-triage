"""OpenAI-compatible generative adapter for benchmark v3 (WP4).

Reproduces the pinned application's **native** request/parse path: same system
prompt, same header/``snippet[:1500]`` user body (already rendered by WP2), the
same greedy ``{...}`` JSON parse, and the same single ``retry_max_tokens`` retry
when the finish reason is ``length``.  Raw response text is preserved verbatim;
the parsed result and per-field provenance are recorded separately.

A policy-conditioned case gets its trusted policy card *appended to the system
context* and its hash recorded -- the card genuinely reaches the model rather
than riding along as unused metadata.

Network is opt-in: the adapter refuses to construct without a transport, and an
HTTP endpoint is only accepted when it is loopback unless ``allow_remote=True``.
No API key is ever written to a fingerprint, run manifest or report.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..common.hashing import canonical, sha256_text
from ..contracts import (NATIVE_JSON_MODE, NATIVE_MAX_TOKENS,
                         NATIVE_RETRY_MAX_TOKENS, native_output,
                         parse_native_response, rendered_input_hash)
from ..sandbox import tool_schemas
from . import base as B
from .workflow import run_tool_loop

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def _is_local(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if host in ("localhost", "::1"):
        return True
    return host.startswith("127.")


class OpenAICompatAdapter(B.Adapter):
    adapter_id = "generative-openai"
    revision = "v3.0"
    capabilities = {"decision": True, "prose": True, "tools": True,
                    "native_parse": True}
    mock = False
    qualifies_as_baseline = True
    confidence_meaning = ("single model-reported confidence per the native prompt; "
                          "it is one declared value, not both category and reply "
                          "probability")
    prompt_revision = "native-v3.0"

    def __init__(self, base_url=None, model="", model_revision=None,
                 model_artifact_sha256=None, api_key=None, transport=None,
                 timeout=240, temperature=0.0, allow_remote=False,
                 generation_config=None, runtime_config=None):
        if transport is None and not base_url:
            raise B.AdapterError(
                "generative adapter needs either a transport or a base_url")
        if transport is None and base_url and not _is_local(base_url) and not allow_remote:
            raise B.AdapterError(
                "refusing a non-local endpoint %s without allow_remote=True" % base_url)
        super().__init__(
            generation_config or {
                "temperature": temperature,
                "max_tokens": NATIVE_MAX_TOKENS,
                "retry_max_tokens": NATIVE_RETRY_MAX_TOKENS,
                "json_mode": NATIVE_JSON_MODE,
                "full": True,
            },
            runtime_config or {
                "backend": "openai_compatible",
                "endpoint_host": (urlparse(base_url).hostname if base_url else "injected"),
                "endpoint_local": True if _is_local(base_url or "http://localhost") else bool(allow_remote),
            })
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.model_key = model or "openai-compatible"
        self.model_revision = model_revision
        self.model_artifact_sha256 = model_artifact_sha256
        self._api_key = api_key  # never serialized
        self._transport = transport
        self.timeout = timeout
        self.temperature = temperature
        self.allow_remote = allow_remote

    # ------------------------------------------------------------- transport

    def _chat(self, payload):
        if self._transport is not None:
            response = self._transport(payload)
        else:
            response = self._http_transport(payload)
        if not isinstance(response, dict):
            raise B.AdapterError("transport returned a non-mapping response")
        response.setdefault("request_sha256", sha256_text(canonical(payload)))
        return response

    def _http_transport(self, payload):
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            raise RuntimeError("LLM HTTP %s: %s" % (exc.code, exc.read()[:300])) from exc
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append({"id": tc.get("id") or "", "name": fn.get("name") or "",
                          "arguments": fn.get("arguments") or "{}"})
        return {"content": message.get("content") or "",
                "tool_calls": calls,
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage") or {}}

    # ------------------------------------------------------------- rendering

    def _rendered(self, view):
        rendered = view.get("rendered_input") or {}
        system = rendered.get("system") or ""
        user = rendered.get("user") or ""
        policy_sha = None
        policy_sent = False
        if view.get("input_profile") == "policy_conditioned":
            policy = view.get("policy")
            if isinstance(policy, dict) and policy.get("policy_id"):
                system = (system + "\n\nTRUSTED POLICY CARD\n"
                          + canonical(policy))
                policy_sha = sha256_text(canonical(policy))
                policy_sent = True
        return system, user, policy_sha, policy_sent

    def _decision(self, system, user, params):
        max_tokens = int(params.get("max_tokens") or self.generation_config.get(
            "max_tokens") or NATIVE_MAX_TOKENS)
        retry_max_tokens = int(params.get("retry_max_tokens")
                               or NATIVE_RETRY_MAX_TOKENS)
        json_mode = params.get("json_mode", NATIVE_JSON_MODE)

        def payload_for(tokens):
            p = {"model": self.model, "temperature": self.temperature,
                 "max_tokens": tokens,
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user}]}
            if json_mode:
                p["response_format"] = {"type": "json_object"}
            return p

        payload = payload_for(max_tokens)
        response = self._chat(payload)
        content = response.get("content") or ""
        finish = response.get("finish_reason")
        match = re.search(r"\{.*\}", content, re.S)
        retried = False
        # Mirror engine.LLMClient.classify: retry only on "no JSON object" + length.
        if not match and finish == "length":
            retried = True
            response = self._chat(payload_for(retry_max_tokens))
            content = response.get("content") or ""
            finish = response.get("finish_reason")
        return response, content, finish, retried

    # ---------------------------------------------------------------- run

    def run_case(self, view, sandbox=None):
        system, user, policy_sha, policy_sent = self._rendered(view)
        rendered = view.get("rendered_input") or {}
        params = rendered.get("params") or {}
        semantic_hash = rendered_input_hash(rendered,
                                            mailbox=view.get("mailbox"),
                                            tools=view.get("tools"))
        task = view.get("task")

        if task == "workflow":
            if sandbox is None:
                return B.make_result(status=B.STATUS_ERROR, raw=None, parsed=None,
                                     error="workflow case requires a sandbox",
                                     field_provenance=B.provenance_for(None),
                                     failure_class=B.FAIL_INFRASTRUCTURE)
            names = view.get("tools")
            tools = tool_schemas(names) if names else tool_schemas()

            def turn(system_, messages, tools_):
                p = {"model": self.model, "temperature": self.temperature,
                     "max_tokens": int(params.get("max_tokens") or NATIVE_MAX_TOKENS),
                     "messages": [{"role": "system", "content": system_}] + list(messages),
                     "tools": tools_}
                resp = self._chat(p)
                return {"content": resp.get("content") or "",
                        "tool_calls": resp.get("tool_calls") or [],
                        "usage": resp.get("usage") or {},
                        "request_sha256": resp.get("request_sha256", "")}

            loop = run_tool_loop(turn, sandbox, system, user, tools)
            raw = json.dumps({"turns": loop["raw_turns"], "reply": loop["reply"]},
                             sort_keys=True)
            if loop["error"]:
                status, failure = B.STATUS_ERROR, B.FAIL_INFRASTRUCTURE
            elif loop["budget_exhausted"]:
                # A budget exhaustion is not an infrastructure failure.
                status, failure = B.STATUS_ERROR, B.FAIL_BUDGET
            else:
                status, failure = B.STATUS_OK, None
            parsed = {"task": "workflow", "steps": loop["steps"],
                      "completed": status == B.STATUS_OK,
                      "answer": loop["reply"]}
            return B.make_result(
                status=status, raw=raw, parsed=parsed, error=loop["error"],
                field_provenance=B.provenance_for(None),
                tool_events=loop["tool_events"],
                timings={"measured": "warm", "steps": loop["steps"],
                         "simulated_tools": True, "label": "generative workflow"},
                request_sha256=semantic_hash,
                failure_class=failure,
                output_extra={"wire_sha256": loop["request_sha256"]},
                capabilities_used={"tools": True})

        try:
            response, content, finish, retried = self._decision(system, user, params)
        except Exception as exc:  # noqa: BLE001 - infra failure is distinct from model
            return B.make_result(
                status=B.STATUS_ERROR, raw=None, parsed=None, error=repr(exc),
                field_provenance=B.provenance_for(None),
                failure_class=B.FAIL_INFRASTRUCTURE,
                timings={"measured": "warm", "label": "generative decision"})

        parsed, error = None, None
        parsed_full = None
        try:
            parsed_full = parse_native_response(content)
            parsed = native_output(parsed_full)
        except Exception as exc:  # noqa: BLE001 - preserve exact parser failure
            error = "%s: %s" % (type(exc).__name__, exc)

        uses_prose = bool(parsed and parsed.get("summary") is not None
                          and parsed.get("reason") is not None)
        return B.make_result(
            status=B.STATUS_OK if parsed is not None else B.STATUS_ERROR,
            raw=content, parsed=parsed, error=error,
            field_provenance=B.provenance_for(parsed),
            timings={"measured": "warm", "retried": retried,
                     "finish_reason": finish, "label": "generative decision"},
            request_sha256=semantic_hash,
            failure_class=None if parsed is not None else B.FAIL_MODEL,
            output_extra={"policy_sha256": policy_sha,
                          "policy_transmitted": policy_sent,
                          "wire_sha256": response.get("request_sha256", ""),
                          "parsed_full": parsed_full if parsed is not None else None},
            capabilities_used={"decision": True, "prose": uses_prose,
                               "native_parse": True})

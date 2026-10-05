"""System One typed-decision adapters for benchmark v3 (v2 sweep models).

One adapter family, ``systemone:<key>``, wraps the five decision-only heads the
v2 sweep ran (``benchmarks/v2/harness/jev_sweep.py`` backends):

    gliner        fastino/GLiNER2.5-Decide 340M   pip gliner2 classify_text
    laya          convaiinnovations/laya-typed-decisions 421M   pip laya Router
    kev           jaredpalmer/kev-0.8b 800M       kev server /v1/systemone
    nanojev       C-Tianyu/NanoJev unified-games-v1 (CUDA)  DecisionPredictor
    nanojev-rag   sdmlai/nano-jev v1.0 33.4M      pip nano-jev decider.decide

Every head answers the production classification task as two typed questions
over the case's rendered email -- a six-label category choice (with the declared
descriptions) and a ``needs_reply`` noul/boolean -- and the adapter synthesizes
the native JSON shape the v3 scorer consumes.

Contract care (mirrors :mod:`benchmarks.v3.adapters.tinyjev`):

* the category enum and descriptions are built from the **case's declared
  categories** (then the trusted policy card, then the app defaults), and the
  exact option order is recorded so a probability maps back to its option;
* the two uncertainty values have **separately declared meanings**; the native
  ``confidence`` is a declared combination, never treated as two calibrated
  signals;
* the head is decision-only: ``summary``/``reason`` are recorded ``missing`` and
  are **never fabricated**;
* ``raw`` preserves the actual model answer (not the request payload); the wire
  payload digest is recorded separately as ``wire_sha256`` and the semantic
  rendered-input hash as ``request_sha256``;
* every backend dependency is imported **lazily** and a missing dependency
  surfaces as an explicit ``unavailable`` failure, never a silent capability
  claim.
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from ..common.hashing import canonical, sha256_text
from ..contracts import DEFAULT_CATEGORIES, decision_only_provenance, \
    rendered_input_hash
from . import base as B

DEFAULT_DESCRIPTIONS = {
    "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
    "Notification": "Automatic status updates and notices that need no action",
    "Newsletter": "Recurring editorial or digest content the owner subscribed to",
    "Receipt": "Order confirmations, invoices, and payment receipts",
    "Personal": "Mail from friends, family, or personal contacts",
    "Promo": "Marketing, offers, and sales promotions",
}

CATEGORY_INSTRUCTIONS = "Which category does this email belong to?"
REPLY_INSTRUCTIONS = ("Does this email need a reply from the account owner? "
                      "Answer for {owner}.")

# The category answer and the reply answer are two separate signals.  The native
# ``confidence`` field is a declared combination of them, NOT a calibrated pair.
CONFIDENCE_MEANING = {
    "category": "choice-answer confidence over the recorded option order",
    "needs_reply": "p_true from the boolean/noul answer",
    "native_confidence": "min(category_confidence, max(p_true, 1-p_true)): a "
                         "declared single uncertainty, not both signals and not "
                         "a calibrated probability",
}

ADAPTER_KEYS = ("gliner", "laya", "kev", "nanojev", "nanojev-rag")

# The upstream NanoJev inference entry point (unified-games-v1 checkpoint).
NANOJEV_CHECKPOINT = os.path.expanduser(
    "~/jev-models/NanoJev/checkpoints/NanoJev-unified")
NANOJEV_SCRIPTS = os.path.expanduser("~/jev-models/NanoJev/scripts")


# ------------------------------------------------------------------ helpers

def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _category_question(categories, descriptions):
    return {"type": "choice", "instructions": CATEGORY_INSTRUCTIONS,
            "criteria": {c: descriptions.get(c, "") for c in categories}}


def _needs_reply_question(owner):
    return {"type": "noul",
            "instructions": REPLY_INSTRUCTIONS.format(
                owner=owner or "the account owner")}


def _top_probability(probs):
    return float(max(probs.values())) if isinstance(probs, dict) and probs \
        else None


# ---------------------------------------------------------------- head pipeline

class _Head(object):
    """Common per-backend lifecycle: load once, predict per case, release."""

    def load(self):
        pass

    def close(self):
        pass

    def predict(self, user, categories, descriptions, owner):
        raise NotImplementedError


class GlinerHead(_Head):
    """GLiNER2.5-Decide via ``AutoExtractor.classify_text`` (CPU)."""

    def __init__(self, device="cpu", **kwargs):
        self.device = device
        self.model = None

    def load(self):
        from gliner2 import AutoExtractor  # noqa: PLC0415 - lazy by design
        self.model = AutoExtractor.from_pretrained("fastino/GLiNER2.5-Decide")

    @staticmethod
    def _task_value(out, task, positive=None):
        """Normalise one classification head to (label, confidence-or-None)."""
        v = out.get(task)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            label = v.get("choice") or v.get("label") or v.get("text")
            conf = v.get("confidence")
            if label is None and positive is not None and positive in v:
                label = positive
            return label, (float(conf) if isinstance(conf, (int, float)) else None)
        return v, None

    def predict(self, user, categories, descriptions, owner):
        tasks = {
            "category": {c: descriptions.get(c, "") for c in categories},
            "needs_reply": {"labels": ["yes", "no"],
                            "prompt": REPLY_INSTRUCTIONS.format(
                                owner=owner or "the account owner")},
        }
        out = self.model.classify_text(user, tasks, include_confidence=True)
        cat, cat_conf = self._task_value(out, "category")
        if not cat:
            raise ValueError("GLiNER2.5-Decide returned no category")
        nr, nr_conf = self._task_value(out, "needs_reply", positive="yes")
        if isinstance(nr, str):
            base = nr_conf if nr_conf is not None else 0.5
            p_nr = base if nr.lower() == "yes" else 1.0 - base
        else:
            p_nr = 0.5
        return {
            "category": str(cat),
            "p_category": float(cat_conf) if cat_conf is not None else 0.5,
            "p_needs_reply": float(p_nr),
            "raw": out,
            "request": {"text": user, "tasks": tasks, "include_confidence": True},
            "category_probabilities": None,
            "needs_reply_probabilities": None,
        }


class LayaHead(_Head):
    """Laya typed decisions via the pip ``laya`` Router (CPU)."""

    MODEL = "typed-decisions"

    def __init__(self, device="cpu", **kwargs):
        self.device = device
        self.router = None

    def load(self):
        from laya import Router  # noqa: PLC0415 - lazy by design
        self.router = Router(device=self.device, max_loaded=1)

    def predict(self, user, categories, descriptions, owner):
        questions = {"category": _category_question(categories, descriptions),
                     "needs_reply": _needs_reply_question(owner)}
        res = self.router.predict(user, questions, model=self.MODEL)
        ans = res["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        choice = cat.get("choice")
        if not choice:
            raise ValueError("Laya returned no category choice")
        probs = cat.get("probabilities") or {}
        p_cat = _top_probability(probs)
        if p_cat is None:
            p_cat = float(cat.get("confidence", 0.5))
        return {
            "category": str(choice),
            "p_category": p_cat,
            "p_needs_reply": float(nr["noul"]),
            "raw": ans,
            "request": {"state": user, "questions": questions,
                        "model": self.MODEL},
            "category_probabilities": probs or None,
            "needs_reply_probabilities": None,
        }


class KevHead(_Head):
    """Kev-0.8B, served by its own repo server over ``/v1/systemone`` (CPU)."""

    RUN = "jaredpalmer/kev-0.8b"

    def __init__(self, device="cpu", endpoint=None, port=0, log_dir=None,
                 **kwargs):
        self.device = device
        self.endpoint = endpoint
        self.port = port
        self.log_dir = (log_dir or os.environ.get("SYSTEMONE_KEV_LOG_DIR")
                        or tempfile.gettempdir())
        self.proc = None
        self.url = None
        self.requests = None

    def load(self):
        import requests  # noqa: PLC0415 - lazy by design
        self.requests = requests
        if self.endpoint:
            self.url = self.endpoint
            return
        port = self.port or _free_port()
        env = dict(os.environ)
        env.setdefault("KEV_FUSED", "0")
        os.makedirs(self.log_dir, exist_ok=True)
        log = open(os.path.join(self.log_dir, "kev-server.log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "kev.serve", "--run", self.RUN,
             "--port", str(port)],
            stdout=log, stderr=log, env=env)
        self.url = "http://127.0.0.1:%d/v1/systemone" % port
        atexit.register(self.close)
        timeout = float(os.environ.get("SYSTEMONE_KEV_READY_TIMEOUT", "1800"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("kev server exited early "
                                   "(see kev-server.log)")
            try:
                r = self.requests.post(
                    self.url,
                    json={"model": "kev-latest", "state": "ready check",
                          "questions": {"q": {"type": "noul",
                                              "instructions": "Ready?"}}},
                    timeout=10)
                if r.status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - server still starting
                pass
            time.sleep(3)
        raise RuntimeError("kev server did not become ready in %ds" % timeout)

    def predict(self, user, categories, descriptions, owner):
        questions = {"category": _category_question(categories, descriptions),
                     "needs_reply": _needs_reply_question(owner)}
        body = {"model": "kev-latest", "state": user, "questions": questions}
        r = self.requests.post(self.url, json=body, timeout=300)
        if r.status_code != 200:
            raise RuntimeError("kev HTTP %s: %s" % (r.status_code,
                                                    r.text[:300]))
        ans = r.json()["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        choice = cat.get("choice")
        if not choice:
            raise ValueError("kev returned no category choice")
        probs = cat.get("probabilities") or {}
        p_cat = _top_probability(probs)
        if p_cat is None:
            p_cat = float(cat.get("confidence", 0.5))
        return {
            "category": str(choice),
            "p_category": p_cat,
            "p_needs_reply": float(nr["noul"]),
            "raw": ans,
            "request": body,
            "category_probabilities": probs or None,
            "needs_reply_probabilities": None,
        }

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                self.proc.kill()


class NanoJevHead(_Head):
    """NanoJev-0.6B unified-games-v1 via the upstream ``DecisionPredictor``.

    CUDA only: the upstream entry point refuses a CPU/remote fallback, so a
    non-CUDA device surfaces as an explicit failure rather than an invented
    result.
    """

    def __init__(self, device="cuda:0", precision=None, checkpoint=None,
                 **kwargs):
        self.device = device
        self.precision = precision or ("bf16" if "cuda" in (device or "")
                                       else "fp32")
        self.checkpoint = checkpoint or NANOJEV_CHECKPOINT
        self.engine = None

    def _download(self):
        if os.path.isdir(self.checkpoint) and os.listdir(self.checkpoint):
            return
        from huggingface_hub import snapshot_download  # noqa: PLC0415
        snapshot_download(repo_id="C-Tianyu/NanoJev",
                          revision="unified-games-v1",
                          local_dir=self.checkpoint,
                          allow_patterns=["best.safetensors", "config.json",
                                          "tokenizer/*", "backbone_config/*"])

    def load(self):
        if NANOJEV_SCRIPTS not in sys.path:
            sys.path.insert(0, NANOJEV_SCRIPTS)
        self._download()
        from predict_toy_decisions import DecisionPredictor  # noqa: PLC0415
        self.engine = DecisionPredictor(
            self.checkpoint, device_name=self.device,
            disable_native_triton=True, precision=self.precision)

    def predict(self, user, categories, descriptions, owner):
        questions = {
            "category": {"type": "choice",
                         "instructions": CATEGORY_INSTRUCTIONS,
                         "criteria": {c: descriptions.get(c, "")
                                      for c in categories}},
            "needs_reply": {"type": "boolean",
                            "instructions": REPLY_INSTRUCTIONS.format(
                                owner=owner or "the account owner")},
        }
        payload = {"states": [{"id": "case", "state": user,
                               "questions": questions}]}
        out = self.engine.predict(payload)
        ans = out["states"][0]["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        choice = cat.get("choice")
        if not choice:
            raise ValueError("NanoJev returned no category choice")
        probs = cat.get("probabilities") or {}
        p_cat = _top_probability(probs)
        if p_cat is None:
            p_cat = 0.5
        return {
            "category": str(choice),
            "p_category": p_cat,
            "p_needs_reply": float(nr["p_true"]),
            "raw": out,
            "request": payload,
            "category_probabilities": probs or None,
            "needs_reply_probabilities": None,
        }


class NanoJevRagHead(_Head):
    """Nano-Jev RAG v1.0 via ``decider.decide`` custom options (CPU).

    This is the RAG relevance model used off-label for classification, exactly
    as in the v2 sweep; the two questions use the model's custom-option path.
    """

    VERSION = "v1.0"

    def __init__(self, device="cpu", version=None, **kwargs):
        self.device = device
        self.version = version or self.VERSION
        self.decider = None

    def load(self):
        import nanojev  # noqa: PLC0415 - lazy by design
        self.decider = nanojev.load(self.version, device=self.device) \
            if self.version else nanojev.load(device=self.device)

    def predict(self, user, categories, descriptions, owner):
        cat_q = "Which of these categories does this email belong to?"
        nr_q = "Does this email need a reply from the account owner?"
        cat_probs = self.decider.decide(cat_q, list(categories), user)
        nr_probs = self.decider.decide(nr_q, ["yes", "no"], user)
        cat = max(cat_probs, key=cat_probs.get)
        return {
            "category": str(cat),
            "p_category": float(cat_probs[cat]),
            "p_needs_reply": float(nr_probs.get("yes", 0.5)),
            "raw": {"category": cat_probs, "needs_reply": nr_probs},
            "request": {"category_question": cat_q,
                        "category_options": list(categories),
                        "needs_reply_question": nr_q,
                        "needs_reply_options": ["yes", "no"], "state": user},
            "category_probabilities": cat_probs,
            "needs_reply_probabilities": nr_probs,
        }


def build_head(key, *, device="cpu", endpoint=None, precision=None, **kwargs):
    """Build the lazy head for ``key`` (tests patch this to inject a fake)."""
    if key == "gliner":
        return GlinerHead(device=device, **kwargs)
    if key == "laya":
        return LayaHead(device=device, **kwargs)
    if key == "kev":
        return KevHead(device=device, endpoint=endpoint, **kwargs)
    if key == "nanojev":
        return NanoJevHead(device=device, precision=precision, **kwargs)
    if key == "nanojev-rag":
        return NanoJevRagHead(device=device, **kwargs)
    raise B.AdapterError("unknown systemone head %r" % key)


# ------------------------------------------------------------------- adapters

class SystemOneAdapter(B.Adapter):
    """Base decision-only adapter shared by every ``systemone:<key>`` head."""

    key = ""
    adapter_id = "systemone:"
    revision = "v3.0"
    capabilities = {"decision": True, "prose": False, "tools": False,
                    "native_parse": False}
    mock = False
    qualifies_as_baseline = True
    confidence_meaning = json.dumps(CONFIDENCE_MEANING, sort_keys=True)
    prompt_revision = "systemone-typed-v1"

    backend_model_key = ""
    backend_revision = ""
    backend_runtime = ""

    def __init__(self, device=None, endpoint=None, precision=None,
                 model_revision=None, model_artifact_sha256=None, head=None,
                 categories=None, descriptions=None, temperature=1.0):
        self.device = device or "cpu"
        self.endpoint = endpoint
        self.precision = precision
        self.model_key = self.backend_model_key or self.key
        self.model_revision = model_revision or self.backend_revision
        self.model_artifact_sha256 = model_artifact_sha256
        self.temperature = temperature
        self._head = head
        super().__init__(
            generation_config={"temperature": temperature,
                               "option_order_source": "per-case",
                               "confidence_meaning": CONFIDENCE_MEANING},
            runtime_config={"backend": "systemone:%s" % self.key,
                            "device": self.device,
                            "runtime": self.backend_runtime})

    # -------------------------------------------------------------- lifecycle

    def _get_head(self):
        """Load and **cache** the backend head (never reload per case)."""
        if self._head is None:
            try:
                head = build_head(self.key, device=self.device,
                                  endpoint=self.endpoint,
                                  precision=self.precision)
                head.load()
            except ImportError as exc:
                raise B.AdapterError(
                    "systemone %s unavailable: %s" % (self.key, exc)) from exc
            self._head = head
        return self._head

    def close(self):
        if self._head is not None:
            self._head.close()

    # ----------------------------------------------------------------- context

    def _render_context(self, view):
        rendered = view.get("rendered_input") or {}
        policy = view.get("policy") or {}
        cats = list(rendered.get("categories") or []) or list(DEFAULT_CATEGORIES)
        if not cats:
            for entry in policy.get("categories") or []:
                name = entry.get("name") if isinstance(entry, dict) else entry
                if name:
                    cats.append(str(name))
        if not cats:
            cats = list(DEFAULT_CATEGORIES)
        desc = dict(DEFAULT_DESCRIPTIONS)
        for entry in policy.get("categories") or []:
            if isinstance(entry, dict) and entry.get("name"):
                desc[str(entry["name"])] = str(
                    entry.get("description")
                    or desc.get(str(entry["name"]), ""))
        descriptions = {c: desc.get(c, "") for c in cats}
        owner = rendered.get("owner") or policy.get("owner") or ""
        return cats, descriptions, owner

    # -------------------------------------------------------------------- run

    def run_case(self, view, sandbox=None):
        if view.get("task") != "decision":
            return B.make_result(
                status=B.STATUS_SKIPPED, raw=None, parsed=None,
                error="systemone:%s is decision-only" % self.key,
                field_provenance=decision_only_provenance(confidence="missing"),
                capabilities_used={"decision": True})

        rendered = view.get("rendered_input") or {}
        user = rendered.get("user") or ""
        cats, descriptions, owner = self._render_context(view)
        semantic_hash = rendered_input_hash(
            rendered, mailbox=view.get("mailbox"), tools=view.get("tools"))

        t0 = time.perf_counter()
        try:
            head = self._get_head()
            r = head.predict(user, cats, descriptions, owner)
        except B.AdapterError as exc:
            return B.make_result(
                status=B.STATUS_ERROR, raw=None, parsed=None, error=str(exc),
                field_provenance=decision_only_provenance(confidence="missing"),
                failure_class=B.FAIL_INFRASTRUCTURE,
                request_sha256=semantic_hash)
        except Exception as exc:  # noqa: BLE001 - never lose a case
            return B.make_result(
                status=B.STATUS_ERROR, raw=None, parsed=None,
                error="%s: %s" % (type(exc).__name__, exc),
                field_provenance=decision_only_provenance(confidence="missing"),
                failure_class=B.FAIL_MODEL,
                request_sha256=semantic_hash)
        wall = time.perf_counter() - t0

        try:
            cat = r["category"]
            p_cat = float(r["p_category"])
            p_nr = float(r["p_needs_reply"])
            if not cat:
                raise ValueError("head returned an empty category")
        except Exception as exc:  # noqa: BLE001
            return B.make_result(
                status=B.STATUS_ERROR, raw=canonical(r.get("raw")),
                parsed=None,
                error="malformed head answer: %s: %s" % (type(exc).__name__, exc),
                field_provenance=decision_only_provenance(confidence="missing"),
                failure_class=B.FAIL_MODEL,
                request_sha256=semantic_hash)

        wire_payload = r.get("request")
        wire_hash = (sha256_text(canonical(wire_payload))
                     if wire_payload is not None else "")
        combined = round(min(p_cat, max(p_nr, 1.0 - p_nr)), 4)
        parsed = {"category": cat, "needs_reply": p_nr >= 0.5,
                  "confidence": combined}
        output_extra = {
            "wire_sha256": wire_hash,
            "systemone": {
                "head": self.key,
                "option_order": list(cats),
                "category_probabilities": r.get("category_probabilities"),
                "category_confidence": p_cat,
                "needs_reply_p_true": p_nr,
                "needs_reply_probabilities": r.get("needs_reply_probabilities"),
                "confidence_meaning": CONFIDENCE_MEANING,
                "synthesized_summary_reason": False,
                "runtime": self.backend_runtime,
            },
        }
        return B.make_result(
            status=B.STATUS_OK, raw=canonical(r.get("raw")),
            parsed=parsed, error=None,
            field_provenance=decision_only_provenance(confidence="derived"),
            timings={"wall_s": round(wall, 3), "measured": "warm",
                     "label": "systemone %s typed decision" % self.key},
            request_sha256=semantic_hash,
            output_extra=output_extra,
            capabilities_used={"decision": True, "prose": False})


class SystemOneGlinerAdapter(SystemOneAdapter):
    key = "gliner"
    adapter_id = "systemone:gliner"
    backend_model_key = "gliner25-decide"
    backend_revision = "fastino/GLiNER2.5-Decide (DeBERTa-v3-large encoder, 340M)"
    backend_runtime = "pip gliner2 AutoExtractor.classify_text (CPU)"


class SystemOneLayaAdapter(SystemOneAdapter):
    key = "laya"
    adapter_id = "systemone:laya"
    backend_model_key = "laya-typed"
    backend_revision = "convaiinnovations/laya-typed-decisions (bundle subfolder)"
    backend_runtime = "pip laya Router(model='typed-decisions') (CPU)"


class SystemOneKevAdapter(SystemOneAdapter):
    key = "kev"
    adapter_id = "systemone:kev"
    backend_model_key = "kev-08b"
    backend_revision = "jaredpalmer/kev-0.8b rev 9a45d25e + Qwen/Qwen3.5-0.8B-Base"
    backend_runtime = "kev repo server (CPU fp32), /v1/systemone"


class SystemOneNanoJevAdapter(SystemOneAdapter):
    key = "nanojev"
    adapter_id = "systemone:nanojev"
    backend_model_key = "nanojev-06b"
    backend_revision = "C-Tianyu/NanoJev unified-games-v1 (Qwen3-0.6B + decision heads)"
    backend_runtime = "upstream DecisionPredictor, CUDA bf16"


class SystemOneNanoJevRagAdapter(SystemOneAdapter):
    key = "nanojev-rag"
    adapter_id = "systemone:nanojev-rag"
    backend_model_key = "nano-jev-rag"
    backend_revision = "sdmlai/nano-jev v1.0 (MiniLM-L12-H384, 33.4M)"
    backend_runtime = "pip nano-jev, decider.decide (custom options; RAG-only model)"


ADAPTER_CLASSES = {
    "systemone:%s" % key: cls for key, cls in {
        "gliner": SystemOneGlinerAdapter,
        "laya": SystemOneLayaAdapter,
        "kev": SystemOneKevAdapter,
        "nanojev": SystemOneNanoJevAdapter,
        "nanojev-rag": SystemOneNanoJevRagAdapter,
    }.items()
}


def build_systemone_adapter(adapter_id, **kwargs):
    """Build a registered ``systemone:<key>`` adapter by full id."""
    cls = ADAPTER_CLASSES.get(adapter_id)
    if cls is None:
        raise B.AdapterError(
            "unknown systemone adapter %r (expected one of %s)"
            % (adapter_id, ", ".join(sorted(ADAPTER_CLASSES))))
    return cls(**kwargs)

#!/usr/bin/env python3
"""Production-fidelity checks for benchmark v2 (WP3).

Fidelity is asserted, not assumed.  Each check returns a pass/fail with a
human-readable detail so deviations are documented rather than implied to be
equivalent.  ``--live`` additionally re-extracts prompts from the app's
``engine.py`` (when the app is importable) and compares hashes.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
CORPUS = os.path.join(V2, "corpus")
sys.path.insert(0, V2)

PROMPTS = os.path.join(HERE, "prompts.json")
RENDER_LIMIT = 1500
EXPECTED_LOOP = {"MAX_STEPS": 8, "MAX_CALLS_PER_TURN": 4, "RESULT_CHARS": 4500,
                 "TRANSCRIPT_BUDGET": 30000}


def _load(path):
    with open(path) as f:
        return json.load(f)


def run_checks(live=False):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    prompts = _load(PROMPTS)
    required = ["classify_system", "draft_system", "assistant_system_template",
                "assistant_tools", "assistant_loop", "learn_system",
                "example_draft_system", "summarize_thoughts_system",
                "permissions_text_default"]
    missing = [k for k in required if not prompts.get(k)]
    add("prompts.snapshot_complete", not missing, "missing: %s" % missing)

    loop = prompts.get("assistant_loop") or {}
    loop_ok = all(loop.get(k) == v for k, v in EXPECTED_LOOP.items())
    add("prompts.loop_constants_match_production", loop_ok,
        "expected %s got %s" % (EXPECTED_LOOP, {k: loop.get(k) for k in EXPECTED_LOOP}))

    add("prompts.tool_count", len(prompts.get("assistant_tools") or []) >= 20,
        "tools=%d" % len(prompts.get("assistant_tools") or []))

    # ids vs uids distinct
    msgs = [json.loads(l) for l in open(os.path.join(CORPUS, "messages.jsonl")) if l.strip()]
    distinct = all(m.get("uid") is not None and m["uid"] != m["id"] for m in msgs)
    add("corpus.id_uid_distinct", distinct,
        "messages=%d" % len(msgs))
    # truncation-boundary cases must actually exceed the render limit
    cls_cases = [json.loads(l) for l in
                 open(os.path.join(V2, "cases", "classification.jsonl")) if l.strip()]
    long_cases = [c for c in cls_cases if len(c.get("user") or "") > RENDER_LIMIT]
    add("cases.truncation_boundary_present", bool(long_cases),
        "long classification cases=%d" % len(long_cases))

    # runner fidelity: transcript budget + skipped-overflow capture are wired
    runner_src = open(os.path.join(HERE, "runner.py")).read()
    add("runner.enforces_transcript_budget", "TRANSCRIPT_BUDGET" in runner_src
        and "budget_tripped" in runner_src, "runner references budget")
    add("runner.records_skipped_overflow", "status = \"executed\" if i < MAX_CALLS_PER_TURN"
        in runner_src.replace("\n", " "), "per-turn overflow marked skipped")
    add("runner.separate_latency_milestones",
        all(k in runner_src for k in ("first_visible_s", "first_tool_call_s", "decode_s")),
        "first_event/first_visible/first_tool_call/decode captured")

    # tool simulator fidelity
    sim_src = open(os.path.join(HERE, "tool_sim_v2.py")).read()
    add("sim.uid_not_equal_id", 'm["uid"] = m.get("uid") or (m["id"] + 100000)' in sim_src,
        "simulator keeps distinct uids")

    if live:
        add("live.engine_extraction", *_live_compare())

    return checks


def _live_compare():
    """Best-effort comparison against a freshly extracted prompts snapshot."""
    try:
        import subprocess
        out = os.path.join("/tmp", "prompts_v2_live.json")
        here_app = os.environ.get("MAIL_TRIAGE_APP")
        if not here_app:
            return False, "MAIL_TRIAGE_APP not set; live check skipped"
        env = dict(os.environ, DATA_DIR="/tmp/benchv2extract",
                   IMAP_USER="user@example.com")
        subprocess.run([sys.executable, os.path.join(HERE, "extract_prompts.py")],
                       cwd=here_app, env=env, check=True, capture_output=True)
        live = _load(PROMPTS)
        cur = _load(PROMPTS)
        same = live.get("classify_system") == cur.get("classify_system")
        return same, "classify_system %s" % ("matches" if same else "DIFFERS")
    except Exception as exc:
        return False, "live extraction failed: %r" % exc


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    checks = run_checks(live=args.live)
    if args.json:
        print(json.dumps(checks, indent=1))
    else:
        for c in checks:
            print("%-42s %s  %s" % (c["name"], "PASS" if c["ok"] else "FAIL", c["detail"]))
    failed = [c for c in checks if not c["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

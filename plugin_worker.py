#!/usr/bin/env python3
"""Sandboxed plugin interpreter process. Spawned by plugin_rt (one per plugin).

Protocol (newline-delimited JSON; see plugin_rt.py for the parent side):

  parent -> worker:
    {"cmd": "load", "dir": ..., "entry": ..., "memory_mb": 64, "plugin_id": ..., "version": ...}
    {"cmd": "invoke", "seq": N, "tool": ..., "args": {...}, "deadline_ms": ...}
    {"host_result": {"rid": R, "out": "<json string>"}}
  worker -> parent:
    {"ready": true}
    {"loaded": true} | {"loaded": false, "error": {...}}
    {"host": "<name>", "payload": "<json string>", "rid": R}      (during execute)
    {"seq": N, "result": {...}} | {"seq": N, "error": {"code": ..., "message": ...}}

Enforcement split: this process enforces MEMORY (the interpreter's memory cap);
the parent enforces the wall clock by killing this process at the deadline.
No time limit is set on the QuickJS context on purpose: the python-quickjs
binding refuses host callbacks while its time-limit watchdog is active
("Can not call into Python with a time limit set", verified 2026-10-02), so
timeouts are the supervisor's job - which also makes them un-bypassable.
"""
import json
import os
import sys


def main():
    try:
        import quickjs
    except Exception as exc:
        sys.stdout.write(json.dumps({"ready": False, "error": "quickjs unavailable: %r" % exc}) + "\n")
        sys.stdout.flush()
        return 2

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    ctx = None
    rid = [0]
    stdin = sys.stdin
    DEBUG = os.environ.get("MT_PLUGIN_DEBUG")

    def dbg(msg):
        if DEBUG:
            try:
                with open("/tmp/mt_plugin_debug.log", "a", encoding="utf-8") as fh:
                    fh.write("[worker %d] %s\n" % (os.getpid(), msg))
            except OSError:
                pass

    def host_bridge(name, payload):
        rid[0] += 1
        dbg("bridge call %r rid=%d" % (name, rid[0]))
        try:
            send({"host": str(name), "payload": str(payload), "rid": rid[0]})
            dbg("bridge sent, awaiting host_result")
            line = stdin.readline()
            dbg("bridge got %r" % ((line or "")[:160],))
            if not line:
                return json.dumps({"__error": {"code": "internal",
                                               "message": "host bridge closed"}})
            msg = json.loads(line)
            envelope = msg.get("host_result") or {}
            if int(envelope.get("rid") or 0) != rid[0]:
                return json.dumps({"__error": {"code": "internal",
                                               "message": "host bridge protocol error"}})
            return str(envelope.get("out") or "{}")
        except Exception as exc:
            dbg("bridge exception: %r" % (exc,))
            return json.dumps({"__error": {"code": "internal",
                                           "message": "bridge: %r" % exc}})

    send({"ready": True})
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        c = cmd.get("cmd")
        try:
            if c == "load":
                ctx = quickjs.Context()
                ctx.set_memory_limit(max(8, int(cmd.get("memory_mb") or 64)) * 1048576)
                ctx.add_callable("__host", host_bridge)
                sdk = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "sdk", "runtime.js")
                with open(sdk, "r", encoding="utf-8") as fh:
                    ctx.eval(fh.read())
                with open(os.path.join(cmd["dir"], cmd["entry"]), "r", encoding="utf-8") as fh:
                    ctx.eval(fh.read())
                ctx.eval("__mt_boot()")
                send({"loaded": True})
            elif c == "invoke":
                call = {"tool": cmd.get("tool"), "args": cmd.get("args") or {},
                        "deadline_ms": int(cmd.get("deadline_ms") or 5000)}
                raw = ctx.eval("__mt_exec(%s)" % json.dumps(json.dumps(call)))
                result = json.loads(raw) if isinstance(raw, str) else raw
                send({"seq": cmd.get("seq"),
                      "result": result if isinstance(result, dict) else
                      {"ok": False, "summary": "plugin returned a non-object result"}})
            elif c == "classify":
                raw = ctx.eval("__mt_classify(%s)" % json.dumps(json.dumps(cmd.get("input") or {})))
                result = json.loads(raw) if isinstance(raw, str) else raw
                send({"seq": cmd.get("seq"),
                      "result": result if isinstance(result, dict) else None})
            elif c in ("matcher", "draft", "rank", "event"):
                fn = {"matcher": "__mt_match", "draft": "__mt_draft",
                      "rank": "__mt_rank", "event": "__mt_event"}[c]
                raw = ctx.eval("%s(%s)" % (fn, json.dumps(json.dumps(cmd.get("input") or {}))))
                result = json.loads(raw) if isinstance(raw, str) else raw
                send({"seq": cmd.get("seq"), "result": result})
            elif c == "unload":
                ctx = None
                send({"unloaded": True})
            elif c == "ping":
                send({"pong": True})
        except Exception as exc:
            msg = " ".join(str(exc).split())
            code = "timeout" if "interrupted" in msg else \
                   ("quota" if "out of memory" in msg.lower() else "internal")
            if c == "load":
                send({"loaded": False, "error": {"code": "load", "message": msg[:300]}})
            elif c == "invoke":
                send({"seq": cmd.get("seq"), "error": {"code": code, "message": msg[:300]}})
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

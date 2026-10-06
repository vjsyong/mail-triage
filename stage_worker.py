"""Stage worker: runs CPU-heavy pipeline stages outside the web process.

The Flask app, the fetch worker and the mailbox proxy share one Python process
and therefore one GIL. A long index rebuild (in-process FastEmbed/ONNX) or the
learning refine loop would make the UI crawl while it runs. This module is the
supervised child process that owns those stages instead:

  * claims durable `index` jobs from the stage queue (docs/pipeline-queue.md),
    runs the pass with the same rag.Indexer code, and schedules the incremental
    refresh cadence itself;
  * runs the heuristic auto-refine loop;
  * publishes live state to `stage_state` so the dashboard shows progress and
    handles Index now / Rebuild across the process boundary.

CPU placement is controlled with STAGE_CPUS (e.g. "4-7,10") and STAGE_NICE.
The web process stays responsive because this process has its own GIL and can
be pinned/nice'd to a slice of the machine.
"""

import os
import signal
import subprocess
import sys
import threading
import time

import heuristics
import rag
import store

CHILD_ARG = "--stage-child"
INDEX_KIND = "index"
LEARN_INTERVAL = 300     # seconds between heuristic auto-refine checks
PUBLISH_INTERVAL = 1.0   # seconds between stage_state publishes
SCHEDULE_POLL = 2.0      # seconds between index job claims


def enabled():
    """Whether app.py should run the stages in a child process (default yes)."""
    return (os.environ.get("STAGE_WORKER") or "on").strip().lower() not in (
        "off", "0", "false", "no")


def enqueue_index(rebuild=False, manual=True, not_before=0):
    """Queue one index pass (singleton job: message_id=0). Manual jobs promote
    an already queued scheduled pass so a click is never starved."""
    return store.enqueue_job(
        INDEX_KIND, 0, {"manual": bool(manual), "rebuild": bool(rebuild)},
        priority=10 if manual else 0, not_before=not_before, promote=bool(manual))


def process_index_job(indexer, job):
    """Run one claimed index job to completion and publish the resulting state.
    Returns the state dict."""
    payload = job.get("payload") or {}
    indexer.rebuild_next = bool(payload.get("rebuild"))
    indexer._run(continuous=True)
    state = dict(indexer.state)
    try:
        store.set_stage_state(INDEX_KIND, state)
    except Exception:
        pass
    return state


def _parse_cpus(spec):
    cores = set()
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cores.update(range(int(lo), int(hi) + 1))
        else:
            cores.add(int(part))
    return cores


def _apply_cpu_budget():
    """Pin/nice this process so the web process keeps its CPU (best effort)."""
    spec = (os.environ.get("STAGE_CPUS") or "").strip()
    if spec and hasattr(os, "sched_setaffinity"):
        try:
            cores = _parse_cpus(spec)
            if cores:
                os.sched_setaffinity(0, cores)
        except Exception as exc:
            try:
                store.log_event("warn", "stage worker: STAGE_CPUS=%r ignored (%r)"
                                % (spec, exc))
            except Exception:
                pass
    try:
        nice = int(os.environ.get("STAGE_NICE") or "5")
        if nice:
            os.nice(nice)
    except Exception:
        pass


class StatePublisher(threading.Thread):
    """Mirrors a stage's in-process state dict into stage_state for the UI."""

    def __init__(self, kind, get_state, interval=PUBLISH_INTERVAL):
        super().__init__(daemon=True, name="stage-publish-" + kind)
        self.kind = kind
        self.get_state = get_state
        self.interval = interval
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.wait(self.interval):
            try:
                store.set_stage_state(self.kind, self.get_state())
            except Exception:
                pass


class IndexRunner(threading.Thread):
    """Claims index jobs, runs passes, and keeps the refresh cadence."""

    def __init__(self, indexer=None):
        super().__init__(daemon=True, name="stage-index")
        self.stop_flag = threading.Event()
        self.indexer = indexer or rag.Indexer()

    def _schedule(self):
        """Queue an incremental refresh when the configured cadence is due."""
        if not store.get_setting("index_enabled", True):
            return
        try:
            idle_min = max(1, int(store.get_setting("index_refresh_minutes") or 10))
        except (TypeError, ValueError):
            idle_min = 10
        stats = store.job_stats([INDEX_KIND]).get(INDEX_KIND) or {}
        if stats.get("queued") or stats.get("running"):
            return
        last = max(int(self.indexer.state.get("last_ok") or 0),
                   int(store.get_stage_state(INDEX_KIND).get("last_ok") or 0))
        if time.time() - last >= idle_min * 60:
            enqueue_index(rebuild=False, manual=False)

    def run(self):
        store.init_db()
        store.set_stage_state(INDEX_KIND, dict(self.indexer.state))
        publisher = StatePublisher(INDEX_KIND, lambda: dict(self.indexer.state))
        publisher.start()
        store.log_event("info", "stage worker: index runner ready")
        while not self.stop_flag.is_set():
            try:
                self._schedule()
                claimed = store.claim_jobs([INDEX_KIND], {INDEX_KIND: 1},
                                           worker=self.name)
                if not claimed:
                    self.stop_flag.wait(SCHEDULE_POLL)
                    continue
                job = claimed[0]
                payload = job.get("payload") or {}
                store.log_event("debug", "stage index: pass start (rebuild=%s, manual=%s)"
                                % (bool(payload.get("rebuild")), bool(payload.get("manual"))))
                process_index_job(self.indexer, job)
                if self.indexer.state.get("last_error"):
                    store.fail_job(job["id"], str(self.indexer.state.get("last_error")))
                else:
                    store.finish_job(job["id"])
                store.set_stage_state(INDEX_KIND, dict(self.indexer.state))
            except Exception as exc:
                store.log_event("error", "stage index runner crashed: %r" % exc)
                self.stop_flag.wait(3)
        publisher.stop_flag.set()


class LearnRunner(threading.Thread):
    """Owns the heuristic auto-refine loop off the web process."""

    def __init__(self, interval=LEARN_INTERVAL):
        super().__init__(daemon=True, name="stage-learn")
        self.interval = interval
        self.stop_flag = threading.Event()
        self.state = {"running": False, "last_ok": 0, "last_error": None}

    def run(self):
        store.init_db()
        store.set_stage_state("learn", dict(self.state))
        self.stop_flag.wait(20)  # let startup settle
        while not self.stop_flag.is_set():
            self.state["running"] = True
            store.set_stage_state("learn", dict(self.state))
            try:
                if store.get_setting("heuristic_autorefine", True):
                    for _hid, hname, delta in heuristics.auto_refine():
                        store.log_event("info", "heuristic %r retrained (+%d new label(s))"
                                        % (hname, delta))
                self.state.update({"running": False, "last_ok": int(time.time()),
                                   "last_error": None})
            except Exception as exc:
                self.state.update({"running": False, "last_ok": int(time.time()),
                                   "last_error": repr(exc)})
                store.log_event("error", "heuristic auto-refine failed: %r" % exc)
            store.set_stage_state("learn", dict(self.state))
            self.stop_flag.wait(self.interval)


class Supervisor(threading.Thread):
    """App-side supervisor that keeps the stage-worker child process alive."""

    def __init__(self):
        super().__init__(daemon=True, name="stage-supervisor")
        self.proc = None
        self.stop_flag = threading.Event()
        self.state = {"running": False, "pid": None, "restarts": 0, "last_error": None}

    def is_running(self):
        return bool(self.proc is not None and self.proc.poll() is None)

    def run(self):
        store.init_db()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage_worker.py")
        while not self.stop_flag.is_set():
            if not self.is_running():
                if self.proc is not None:
                    self.state["restarts"] += 1
                    store.log_event("warn", "stage worker exited (code %s) - restarting"
                                    % self.proc.returncode)
                try:
                    self.proc = subprocess.Popen([sys.executable, path, CHILD_ARG],
                                                 cwd=os.path.dirname(path),
                                                 env=os.environ.copy())
                    self.state.update({"running": True, "pid": self.proc.pid,
                                       "last_error": None})
                    store.log_event("info", "stage worker started (pid %d)" % self.proc.pid)
                except Exception as exc:
                    self.state.update({"running": False, "last_error": repr(exc)})
                    store.log_event("error", "stage worker failed to start: %r" % exc)
            self.stop_flag.wait(5)
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.state["running"] = False

    def stop(self):
        self.stop_flag.set()


def main():
    store.init_db()
    _apply_cpu_budget()
    stop = threading.Event()

    def _on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    indexer = rag.Indexer()
    index_runner = IndexRunner(indexer)
    learn_runner = LearnRunner()
    index_runner.start()
    learn_runner.start()
    try:
        cfg = rag.embed_config()
        embed_desc = cfg.get("protocol") or "?"
    except Exception:
        embed_desc = "?"
    store.log_event("info", "stage worker running (pid %d, backend=%s, embed=%s)"
                    % (os.getpid(), store.get_setting("rag_backend") or "lite", embed_desc))
    while not stop.is_set():
        stop.wait(1)
    index_runner.stop_flag.set()
    learn_runner.stop_flag.set()
    indexer.stop_flag.set()
    for t in (index_runner, learn_runner):
        t.join(timeout=10)
    store.log_event("info", "stage worker stopped (pid %d)" % os.getpid())


if __name__ == "__main__":
    main()

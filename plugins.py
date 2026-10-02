"""Plugin kernel - discovery, manifest validation, registry, tool schemas.

This is the native side of the plugin system described in
docs/plugin-architecture.md:

- scan builtin + user plugin roots for manifest.json files
- validate manifests (treated as data, never evaluated) against
  schemas/plugin-manifest.schema.json plus semantic rules
- keep the `plugins` registry table in sync (consent state, grants, hashes)
- synthesize OpenAI/Ollama tool schemas for enabled plugins

Execution lives in plugin_rt.py (imported lazily by callers, so discovery,
validation and the CLI all work even when the interpreter package is absent).

Security invariants enforced here:
- a user plugin id cannot use the reserved `mt-` prefix (built-ins only),
  and a built-in id must use it
- `entrypoint` must resolve inside the plugin directory (traversal guard)
- a version bump that grows permissions resets `enabled`/`grants` (re-grant)
- manifest + entrypoint are hashed; same-version changes are flagged
- grants must be a subset of the manifest's declared permissions
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import config
import store

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 1 << 20
RESERVED_PREFIX = "mt-"
HOST_SDK_VERSION = "0.1.0"
VALID_KINDS = ("tool", "classifier", "matcher", "draft-provider", "retriever", "integration")

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "schemas", "plugin-manifest.schema.json")
_schema_cache = None


def manifest_schema():
    global _schema_cache
    if _schema_cache is None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            _schema_cache = json.load(fh)
    return _schema_cache


# ---------------------------------------------------------------- paths

def builtin_root():
    return config.PLUGINS_BUILTIN_DIR


def user_root():
    return config.PLUGINS_DIR


def ensure_user_root():
    os.makedirs(user_root(), exist_ok=True)
    return user_root()


# ---------------------------------------------------------------- versions

def _parse_version(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def sdk_range_ok(expr, actual=HOST_SDK_VERSION):
    """Minimal semver-range check good enough for engines.sdk: space-separated
    comparators (>=, >, <=, <, =, ^, ~), e.g. '>=0.1 <1.0' or '^0.1.0'."""
    av = _parse_version(actual)
    if av is None:
        return False
    tokens = (expr or "").split()
    if not tokens:
        return False
    for token in tokens:
        m = re.match(r"^(>=|<=|>|<|\^|~|=)?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", token)
        if not m:
            return False
        op = m.group(1) or "="
        nums = (int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0))
        if op == ">=" and not av >= nums:
            return False
        if op == ">" and not av > nums:
            return False
        if op == "<=" and not av <= nums:
            return False
        if op == "<" and not av < nums:
            return False
        if op == "=" and not av == nums:
            return False
        if op == "^":
            hi = (nums[0] + 1, 0, 0) if nums[0] else (0, nums[1] + 1, 0)
            if not (av >= nums and av < hi):
                return False
        if op == "~":
            hi = (nums[0], nums[1] + 1, 0)
            if not (av >= nums and av < hi):
                return False
    return True


# ---------------------------------------------------------------- validation

def _fmt_error(err):
    loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return "%s: %s" % (loc, err.message)


def _check_tool_schema(t):
    errs = []
    name = t.get("name") or "?"
    p = t.get("parameters")
    if not isinstance(p, dict) or p.get("type") != "object" \
            or not isinstance(p.get("properties", {}), dict):
        return ["tool '%s': parameters must be a JSON Schema object with properties" % name]
    bad = []

    def walk(node, path):
        if isinstance(node, dict):
            for k in ("$ref", "$defs", "oneOf", "anyOf", "allOf", "not"):
                if k in node:
                    bad.append(path + k)
            for k2, v in node.items():
                walk(v, path + str(k2) + ".")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]." % (path, i))

    walk(p, "")
    if bad:
        errs.append("tool '%s': unsupported schema keywords: %s"
                    % (name, ", ".join(sorted(set(bad))[:5])))
    return errs


def validate_semantics(m, root_kind):
    """Checks the JSON Schema cannot express. Returns a list of error strings."""
    errs = []
    pid = m.get("id") or ""
    if pid.startswith(RESERVED_PREFIX) and root_kind != "builtin":
        errs.append("id '%s': the '%s' prefix is reserved for built-in plugins"
                    % (pid, RESERVED_PREFIX))
    if root_kind == "builtin" and not pid.startswith(RESERVED_PREFIX):
        errs.append("built-in plugin ids must start with '%s' (got '%s')"
                    % (RESERVED_PREFIX, pid))
    if not sdk_range_ok((m.get("engines") or {}).get("sdk")):
        errs.append("engines.sdk '%s' does not accept host SDK %s"
                    % ((m.get("engines") or {}).get("sdk"), HOST_SDK_VERSION))
    perms = m.get("permissions") or []
    if "net.http" in perms:
        hosts = (m.get("net") or {}).get("hosts") or []
        if not hosts:
            errs.append("permission net.http requires net.hosts (non-empty)")
    req_llm = m.get("required_llm_capability") or "none"
    if req_llm != "none" and req_llm not in perms:
        errs.append("required_llm_capability '%s' must also appear in permissions" % req_llm)
    kinds = m.get("kind") or []
    tools = m.get("tools") or []
    if "tool" in kinds and not tools:
        errs.append("kind 'tool' requires a non-empty tools[]")
    names = [t.get("name") for t in tools]
    if len(names) != len(set(names)):
        errs.append("tool names must be unique within a plugin")
    for t in tools:
        errs += _check_tool_schema(t)
    if "classifier" in kinds:
        c = m.get("classifier") or {}
        if not c.get("outputs"):
            errs.append("classifier plugins must declare classifier.outputs (labels)")
    return errs


def load_manifest(pdir, root_kind):
    """Read + validate a plugin directory.

    Returns (manifest|None, manifest_sha256, entry_sha256, errors[]).
    The manifest is only usable when errors is empty.
    """
    mpath = os.path.join(pdir, MANIFEST_NAME)
    try:
        with open(mpath, "rb") as fh:
            raw = fh.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        return None, "", "", ["%s: %s" % (MANIFEST_NAME, exc)]
    if len(raw) > MAX_MANIFEST_BYTES:
        return None, "", "", ["manifest.json larger than 1 MB"]
    try:
        m = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, "", "", ["manifest.json is not valid JSON: %s" % exc]
    if not isinstance(m, dict):
        return None, "", "", ["manifest.json must be a JSON object"]
    msha = hashlib.sha256(raw).hexdigest()
    try:
        import jsonschema
    except ImportError:
        return None, msha, "", ["jsonschema package missing on the host"]
    validator = jsonschema.Draft202012Validator(manifest_schema())
    schema_errs = sorted(validator.iter_errors(m),
                         key=lambda e: [str(p) for p in e.absolute_path])
    if schema_errs:
        return None, msha, "", [_fmt_error(e) for e in schema_errs[:8]]
    errors = validate_semantics(m, root_kind)
    entry = m.get("entrypoint") or ""
    base = os.path.realpath(pdir)
    epath = os.path.realpath(os.path.join(pdir, entry))
    if not (epath == base or epath.startswith(base + os.sep)):
        errors.append("entrypoint '%s' escapes the plugin directory" % entry)
    elif not epath.endswith(".js"):
        errors.append("entrypoint must be a .js bundle")
    elif not os.path.isfile(epath):
        errors.append("entrypoint '%s' not found" % entry)
    entry_sha = ""
    if os.path.isfile(epath):
        with open(epath, "rb") as fh:
            entry_sha = hashlib.sha256(fh.read()).hexdigest()
    if errors:
        return None, msha, entry_sha, errors
    return m, msha, entry_sha, []


# ---------------------------------------------------------------- scan / registry

def _row_to_dict(row):
    d = dict(row)
    try:
        d["manifest"] = json.loads(d.pop("manifest_json") or "{}")
    except (TypeError, ValueError):
        d["manifest"] = {}
        d.pop("manifest_json", None)
    try:
        d["grants"] = json.loads(d.get("grants_json") or "[]")
    except (TypeError, ValueError):
        d["grants"] = []
    d.pop("grants_json", None)
    return d


def get(plugin_id):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM plugins WHERE id=?", (plugin_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_rows(enabled_only=False):
    q = "SELECT * FROM plugins ORDER BY root, id"
    if enabled_only:
        q = "SELECT * FROM plugins WHERE enabled=1 ORDER BY root, id"
    with store.db() as conn:
        return [_row_to_dict(r) for r in conn.execute(q)]


def scan():
    """Discover plugins in both roots and sync the registry.

    Runs at app boot and on demand (`--plugins rescan`, Plugins page button).
    Never raises for a broken plugin: problems land in the report and, for
    known ids, in the row's last_error.
    """
    ensure_user_root()
    now = int(time.time())
    report = {"roots": {"builtin": builtin_root(), "user": user_root()},
              "found": [], "errors": [], "removed_dirs": []}
    seen = {}
    with store.db() as conn:
        known = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM plugins")}
        for root_kind, root in (("builtin", builtin_root()), ("user", user_root())):
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                pdir = os.path.join(root, name)
                if not os.path.isdir(pdir) or not os.path.isfile(os.path.join(pdir, MANIFEST_NAME)):
                    continue
                m, msha, esha, errors = load_manifest(pdir, root_kind)
                if m is None:
                    report["errors"].append({"dir": pdir, "errors": errors})
                    hit = next((pid for pid, r in known.items()
                                if r.get("dir") and os.path.realpath(r["dir"]) == os.path.realpath(pdir)),
                               None)
                    if hit:
                        conn.execute("UPDATE plugins SET last_error=?, updated_ts=? WHERE id=?",
                                     ("; ".join(errors[:3]), now, hit))
                    continue
                pid = m["id"]
                if pid in seen:
                    report["errors"].append(
                        {"dir": pdir, "errors": ["duplicate id '%s' (already loaded from %s)"
                                                 % (pid, seen[pid])]})
                    continue
                seen[pid] = pdir
                row = known.get(pid)
                if row is None:
                    conn.execute(
                        "INSERT INTO plugins (id, version, root, dir, manifest_json, "
                        "manifest_sha256, entry_sha256, enabled, grants_json, last_error, "
                        "installed_ts, updated_ts) VALUES (?,?,?,?,?,?,?,0,'[]','',?,?)",
                        (pid, m["version"], root_kind, pdir, json.dumps(m), msha, esha, now, now))
                    report["found"].append({"id": pid, "version": m["version"], "status": "new"})
                    continue
                status = "unchanged"
                old_manifest = {}
                try:
                    old_manifest = json.loads(row.get("manifest_json") or "{}")
                except (TypeError, ValueError):
                    old_manifest = {}
                perms_new = set(m.get("permissions") or [])
                perms_old = set(old_manifest.get("permissions") or [])
                grew = sorted(perms_new - perms_old)
                if row["version"] != m["version"] and grew:
                    conn.execute(
                        "UPDATE plugins SET version=?, root=?, dir=?, manifest_json=?, "
                        "manifest_sha256=?, entry_sha256=?, enabled=0, grants_json='[]', "
                        "last_error=?, updated_ts=? WHERE id=?",
                        (m["version"], root_kind, pdir, json.dumps(m), msha, esha,
                         "re-grant needed: new permissions " + ", ".join(grew), now, pid))
                    status = "re-grant"
                elif row["version"] != m["version"]:
                    conn.execute(
                        "UPDATE plugins SET version=?, root=?, dir=?, manifest_json=?, "
                        "manifest_sha256=?, entry_sha256=?, last_error='', updated_ts=? WHERE id=?",
                        (m["version"], root_kind, pdir, json.dumps(m), msha, esha, now, pid))
                    status = "updated"
                elif row["manifest_sha256"] != msha or row["entry_sha256"] != esha:
                    conn.execute(
                        "UPDATE plugins SET root=?, dir=?, manifest_json=?, manifest_sha256=?, "
                        "entry_sha256=?, last_error=?, updated_ts=? WHERE id=?",
                        (root_kind, pdir, json.dumps(m), msha, esha,
                         "modified on disk since install (same version)", now, pid))
                    status = "modified"
                else:
                    conn.execute("UPDATE plugins SET root=?, dir=? WHERE id=?",
                                 (root_kind, pdir, pid))
                report["found"].append({"id": pid, "version": m["version"], "status": status})
        for pid, r in known.items():
            if pid not in seen and r.get("dir") and not os.path.isdir(r["dir"]):
                conn.execute("UPDATE plugins SET last_error=? WHERE id=?",
                             ("directory not found on disk", pid))
                report["removed_dirs"].append(pid)
        conn.commit()
    if report["errors"] or report["found"]:
        store.log_event("plugin",
                        "scan: %d plugin dir(s), %d error(s)"
                        % (len(report["found"]), len(report["errors"])))
    return report


# ---------------------------------------------------------------- consent / grants

def set_enabled(plugin_id, enabled, grants=None):
    """Enable/disable a plugin. Enabling with grants=None consents to every
    declared permission (the Plugins page shows them individually)."""
    row = get(plugin_id)
    if not row:
        return {"ok": False, "error": "unknown plugin '%s'" % plugin_id}
    if enabled:
        declared = row["manifest"].get("permissions") or []
        wanted = list(grants) if grants is not None else list(declared)
        bad = [g for g in wanted if g not in declared]
        if bad:
            return {"ok": False, "error": "cannot grant undeclared permission(s): %s"
                                           % ", ".join(bad)}
        with store.db() as conn:
            conn.execute("UPDATE plugins SET enabled=1, grants_json=?, last_error='', "
                         "updated_ts=? WHERE id=?",
                         (json.dumps(wanted), int(time.time()), plugin_id))
        store.log_event("plugin", "enabled '%s' (grants: %s)"
                        % (plugin_id, ", ".join(wanted) or "none"))
        return {"ok": True, "id": plugin_id, "enabled": True, "grants": wanted}
    with store.db() as conn:
        conn.execute("UPDATE plugins SET enabled=0, updated_ts=? WHERE id=?",
                     (int(time.time()), plugin_id))
    store.log_event("plugin", "disabled '%s'" % plugin_id)
    return {"ok": True, "id": plugin_id, "enabled": False}


def set_grants(plugin_id, grants):
    row = get(plugin_id)
    if not row:
        return {"ok": False, "error": "unknown plugin '%s'" % plugin_id}
    declared = row["manifest"].get("permissions") or []
    bad = [g for g in grants if g not in declared]
    if bad:
        return {"ok": False, "error": "cannot grant undeclared permission(s): %s"
                                       % ", ".join(bad)}
    with store.db() as conn:
        conn.execute("UPDATE plugins SET grants_json=?, updated_ts=? WHERE id=?",
                     (json.dumps(list(grants)), int(time.time()), plugin_id))
    return {"ok": True, "id": plugin_id, "grants": list(grants)}


def has_grant(plugin_id, grant):
    row = get(plugin_id)
    return bool(row and row["enabled"] and grant in row["grants"])


def set_last_error(plugin_id, message):
    with store.db() as conn:
        conn.execute("UPDATE plugins SET last_error=?, updated_ts=? WHERE id=?",
                     (str(message)[:300], int(time.time()), plugin_id))
    return True


def disable_with_error(plugin_id, message):
    """Turn a plugin off after runtime failures and record why (audited)."""
    with store.db() as conn:
        conn.execute("UPDATE plugins SET enabled=0, last_error=?, updated_ts=? WHERE id=?",
                     (str(message)[:300], int(time.time()), plugin_id))
    store.log_event("warn", "plugin '%s' auto-disabled: %s" % (plugin_id, str(message)[:200]))
    return {"ok": True, "id": plugin_id, "enabled": False}


# ---------------------------------------------------------------- tool schemas

def tool_full_name(plugin_id, tool_name):
    return "plugin__%s__%s" % (plugin_id, tool_name)


def split_tool_name(name):
    """'plugin__acme-x__find' -> ('acme-x', 'find') or None if not ours."""
    if not isinstance(name, str) or not name.startswith("plugin__"):
        return None
    rest = name[len("plugin__"):]
    if "__" not in rest:
        return None
    pid, _, tool = rest.partition("__")
    if not pid or not tool:
        return None
    return pid, tool


def capability_for_tool(name):
    sp = split_tool_name(name)
    return "plugin:%s" % sp[0] if sp else None


def tool_schemas(query_text=None, budget=None):
    """OpenAI/Ollama function schemas for enabled plugins' assistant tools.

    Budget: settings.plugin_tools_budget caps how many plugin schemas are
    offered per model turn; when over budget and a query text is available,
    tools rank by keyword overlap (name + description + when_to_use).
    """
    if not store.get_setting("plugins_enabled", 1):
        return []
    try:
        budget = int(budget if budget is not None
                     else store.get_setting("plugin_tools_budget", 8) or 8)
    except (TypeError, ValueError):
        budget = 8
    entries = []
    for row in list_rows(enabled_only=True):
        m = row["manifest"]
        when = ((m.get("assistant") or {}).get("when_to_use") or "")
        for t in (m.get("tools") or []):
            if (t.get("surface") or "assistant") != "assistant":
                continue
            entries.append({
                "type": "function",
                "function": {
                    "name": tool_full_name(row["id"], t["name"]),
                    "description": "%s: %s" % (m.get("name") or row["id"], t["description"]),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
                "_score": _keyword_score(query_text, " ".join([
                    t.get("name") or "", t.get("description") or "",
                    when, m.get("description") or ""])),
            })
    if len(entries) > budget and query_text:
        entries.sort(key=lambda e: (-e["_score"], e["function"]["name"]))
        entries = entries[:budget]
    elif len(entries) > budget:
        entries = entries[:budget]
    for e in entries:
        e.pop("_score", None)
    return entries


def _keyword_score(query_text, blob):
    if not query_text:
        return 0
    words = set(re.findall(r"[a-z0-9]{3,}", (query_text or "").lower()))
    hay = set(re.findall(r"[a-z0-9]{3,}", (blob or "").lower()))
    return len(words & hay)


# ---------------------------------------------------------------- CLI

def cli(argv):
    """`python app.py --plugins <cmd>` entry point. Returns a JSON-able dict."""
    args = [a for a in (argv or []) if a]
    cmd = args[0] if args else "list"
    if cmd == "list":
        rows = list_rows()
        return {"roots": {"builtin": builtin_root(), "user": user_root()},
                "sdk_version": HOST_SDK_VERSION,
                "plugins": [{"id": r["id"], "version": r["version"], "root": r["root"],
                             "enabled": bool(r["enabled"]), "grants": r["grants"],
                             "permissions": r["manifest"].get("permissions") or [],
                             "kinds": r["manifest"].get("kind") or [],
                             "last_error": r["last_error"]} for r in rows]}
    if cmd == "validate":
        if len(args) < 2:
            return {"ok": False, "error": "usage: --plugins validate <plugin-dir>"}
        d = args[1]
        kind = "builtin" if os.path.realpath(d).startswith(
            os.path.realpath(builtin_root()) + os.sep) else "user"
        m, msha, esha, errors = load_manifest(d, kind)
        return {"ok": not errors, "dir": d, "root_kind": kind,
                "id": (m or {}).get("id"), "version": (m or {}).get("version"),
                "errors": errors}
    if cmd == "rescan":
        return scan()
    if cmd in ("enable", "disable"):
        if len(args) < 2:
            return {"ok": False, "error": "usage: --plugins %s <id>" % cmd}
        return set_enabled(args[1], cmd == "enable")
    if cmd == "grant":
        if len(args) < 3:
            return {"ok": False, "error": "usage: --plugins grant <id> <permission> [permission...]"}
        return set_grants(args[1], args[2:])
    if cmd == "invoke":
        if len(args) < 3:
            return {"ok": False, "error": "usage: --plugins invoke <id> <tool> [json-args]"}
        try:
            targs = json.loads(args[3]) if len(args) > 3 else {}
        except ValueError:
            return {"ok": False, "error": "args must be JSON"}
        import plugin_rt
        return plugin_rt.runtime.invoke(args[1], args[2], targs)
    return {"ok": False, "error": "unknown command '%s'" % cmd,
            "usage": "list | validate <dir> | rescan | enable|disable <id> | grant <id> <perm...>"}

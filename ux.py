"""Small presentation helpers for the mail workbench (no persistent state)."""
import json


def rule_diagnostics(rules):
    """Only claim proven overlap: identical normalized condition sets and mode.

    Different regexes/contains predicates are not assumed to imply each other.
    Disabled predecessors cannot shadow an enabled rule.
    """
    seen = {}
    result = {}
    for row in rules:
        conds = json.loads(row.get('conditions') or '[]')
        key = (row.get('match_mode') or 'all',
               tuple(sorted(json.dumps(c, sort_keys=True) for c in conds)))
        previous = seen.get(key)
        if previous and conds:
            same_actions = json.loads(previous.get('actions') or '{}') == json.loads(row.get('actions') or '{}')
            result[row['id']] = ('Duplicate of' if same_actions else 'Shadowed by') + ' #%s “%s” — earlier enabled rule has the same conditions.' % (previous['id'], previous['name'])
        if row.get('enabled') and key not in seen:
            seen[key] = row
    return result


# ---------------------------------------------------------------- automation workspace

# Exact GET list paths -> workspace section. Unknown `/automation/*` subpaths
# (notably `/automation/preview`) deliberately stay unchromed.
_AUTOMATION_SECTION_PATHS = {
    "/automation": "overview",
    "/automation/categories": "categories",
    "/automation/controls": "controls",
    "/rules": "rules",
    "/rules/new": "rules",
    "/flows": "flows",
    "/flows/new": "flows",
    "/templates": "drafting",
    "/templates/new": "drafting",
}
_AUTOMATION_EDITOR_SECTIONS = {"rules": "rules", "flows": "flows", "templates": "drafting"}


def automation_section(path):
    """Workspace section id for a request path, or None when it is off-workspace.

    Recognises the new Automation pages plus the canonical rule/flow/template
    list and integer editor paths, so the shared chrome follows existing
    bookmarked URLs. It never authorises anything; it only selects chrome.
    """
    if not path:
        return None
    clean = str(path).split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
    section = _AUTOMATION_SECTION_PATHS.get(clean)
    if section:
        return section
    parts = clean.strip("/").split("/")
    if len(parts) == 3 and parts[1].isdigit() and parts[2] == "edit":
        return _AUTOMATION_EDITOR_SECTIONS.get(parts[0])
    return None


def _decode_object_list(raw, label):
    """Decode a stored JSON array of objects. Returns (value, error or None)."""
    if raw is None or raw == "":
        return [], None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None, "%s is not valid JSON" % label
    if not isinstance(value, list):
        return None, "%s is not a list" % label
    if any(not isinstance(item, dict) for item in value):
        return None, "%s contains a non-object entry" % label
    return value, None


def _name_key(value):
    return str(value or "").strip().lower()


def _flow_category_names(flow):
    """Lowercased category values a flow references, or None if JSON is broken."""
    conds, error = _decode_object_list(flow.get("conditions"), "conditions")
    if error:
        return None
    names = set()
    for cond in conds:
        if (cond.get("kind") or "field").lower() == "category":
            key = _name_key(cond.get("value"))
            if key:
                names.add(key)
    return names


def category_rows(settings, flows, heuristics):
    """Configured vocabulary rows first, then map-only rows in stored map order.

    Each row is `{name, configured, mapping_present, folder, flow_refs,
    classifier_refs}`. Malformed flow JSON is skipped here (diagnostics live in
    `automation_references`). Incompatible top-level settings types raise
    ``ValueError`` rather than silently dropping data.
    """
    cats = settings.get("categories")
    folders = settings.get("category_folders")
    if cats is None:
        cats = []
    if folders is None:
        folders = {}
    if not isinstance(cats, list):
        raise ValueError("Stored 'categories' setting is not a list; repair it before editing categories.")
    if not isinstance(folders, dict):
        raise ValueError("Stored 'category_folders' setting is not a mapping; repair it before editing categories.")
    for name in cats:
        if not isinstance(name, str):
            raise ValueError("Stored 'categories' contains a non-text entry (%r); repair it before editing categories." % (name,))
    for key, value in folders.items():
        if not isinstance(value, str):
            raise ValueError("Stored 'category_folders' has a non-text destination for %r; repair it before editing categories." % (key,))

    flow_map = {}
    for flow in flows or []:
        names = _flow_category_names(flow)
        if names is None:
            continue
        ref = {"id": flow.get("id"), "name": flow.get("name") or "", "enabled": bool(flow.get("enabled"))}
        for key in names:
            flow_map.setdefault(key, []).append(ref)
    clf_map = {}
    for h in heuristics or []:
        key = _name_key(h.get("category"))
        if not key:
            continue
        clf_map.setdefault(key, []).append(
            {"id": h.get("id"), "name": h.get("name") or "", "enabled": bool(h.get("enabled"))})

    rows = []
    for name in cats:
        present = name in folders
        rows.append({
            "name": name,
            "configured": True,
            "mapping_present": present,
            "folder": folders.get(name, "") if present else "",
            "flow_refs": list(flow_map.get(_name_key(name), [])),
            "classifier_refs": list(clf_map.get(_name_key(name), [])),
        })
    for name, folder in folders.items():
        if name in cats:
            continue
        rows.append({
            "name": name,
            "configured": False,
            "mapping_present": True,
            "folder": folder,
            "flow_refs": list(flow_map.get(_name_key(name), [])),
            "classifier_refs": list(clf_map.get(_name_key(name), [])),
        })
    return rows


def automation_references(settings, flows, heuristics):
    """Display references plus diagnostics for flows whose conditions are broken."""
    rows = category_rows(settings, flows, heuristics)
    invalid = []
    for flow in flows or []:
        _, error = _decode_object_list(flow.get("conditions"), "conditions")
        if error:
            invalid.append({"id": flow.get("id"), "name": flow.get("name") or "", "reason": error})
    return {"rows": rows, "invalid_flows": invalid}


def drafting_flows(flows):
    """Flows with any `draft` action, once each, plus malformed-action diagnostics."""
    found = []
    invalid = []
    for flow in flows or []:
        actions, error = _decode_object_list(flow.get("actions"), "actions")
        if error:
            invalid.append({"id": flow.get("id"), "name": flow.get("name") or "", "reason": error})
            continue
        if any((step.get("type") or "").lower() == "draft" for step in actions):
            found.append({"id": flow.get("id"), "name": flow.get("name") or "",
                          "enabled": bool(flow.get("enabled"))})
    return {"flows": found, "invalid_flows": invalid}


def _has_control(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _bit(raw, key):
    value = str(raw or "").strip()
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError("Row field %r must be 0 or 1." % key)


def _posted_flag(raw, key):
    if raw is None:
        return False
    value = str(raw).strip()
    if value in ("", "0"):
        return False
    if value == "1":
        return True
    raise ValueError("Row field %r has an unexpected value." % key)


def parse_category_rows(form, current_settings, references):
    """Parse the indexed category form against current stored state.

    Returns ``(categories, mapping)`` and raises ``ValueError`` with an
    actionable message for any malformed, inconsistent or unsafe input. Never
    trusts posted metadata over the server's current rows; performs no writes.
    """
    current_rows = list((references or {}).get("rows") or [])
    invalid_flows = list((references or {}).get("invalid_flows") or [])
    if current_settings is not None:
        cats = current_settings.get("categories")
        if cats is not None and not isinstance(cats, list):
            raise ValueError("Stored 'categories' setting is not a list; repair it before saving categories.")

    def post(key):
        if hasattr(form, "getlist"):
            got = form.getlist(key)
        else:
            got = [form.get(key)]
        return [v for v in got if v is not None]

    def one(key, required=False):
        values = post(key)
        if len(values) > 1:
            raise ValueError("Form field %r was posted more than once." % key)
        if not values:
            if required:
                raise ValueError("Missing required row field %r." % key)
            return None
        return values[0]

    raw_count = one("row_count", required=True)
    try:
        row_count = int(str(raw_count).strip())
    except (TypeError, ValueError):
        raise ValueError("row_count must be an integer.")
    if row_count < 0:
        raise ValueError("row_count must not be negative.")
    if row_count > 500:
        raise ValueError("row_count is implausibly large.")
    one("settings_version")

    rows = []
    seen = set()
    for i in range(row_count):
        sfx = "_%d" % i
        original_raw = (one("original" + sfx, required=True) or "").strip()
        name_raw = one("name" + sfx, required=True)
        folder_raw = one("folder" + sfx, required=True)
        configured = _bit(one("configured" + sfx, required=True), "configured" + sfx)
        mapping_present = _bit(one("mapping_present" + sfx, required=True), "mapping_present" + sfx)
        remove = _posted_flag(one("remove" + sfx), "remove" + sfx)
        restore = _posted_flag(one("restore" + sfx), "restore" + sfx)
        if remove and restore:
            raise ValueError("Row %d cannot be removed and restored at once." % i)

        server = None
        if original_raw:
            try:
                idx = int(original_raw)
            except (TypeError, ValueError):
                raise ValueError("Row %d has a non-integer original index." % i)
            if idx < 0 or idx >= len(current_rows):
                raise ValueError("Row %d references an out-of-range original row." % i)
            if idx in seen:
                raise ValueError("Original row %d was posted more than once." % idx)
            seen.add(idx)
            server = current_rows[idx]
            if configured != bool(server["configured"]):
                raise ValueError("Row %d changed its configured metadata; reload and retry." % i)
            if mapping_present != bool(server["mapping_present"]):
                raise ValueError("Row %d changed its mapping metadata; reload and retry." % i)
            if name_raw != server["name"]:
                raise ValueError("Row %d tried to rename an existing category; names are read-only." % i)
            if restore and server["configured"]:
                raise ValueError("Row %d tried to restore a category that is already configured." % i)
        else:
            if remove or restore:
                raise ValueError("Row %d cannot remove or restore a new category." % i)
            if not configured or mapping_present:
                raise ValueError("Row %d has invalid metadata for a new category." % i)

        server_folder = server["folder"] if server is not None else ""
        if server is not None and folder_raw == server_folder:
            folder = server_folder
        else:
            folder = folder_raw.strip()
            if _has_control(folder):
                raise ValueError("Row %d has control characters in its destination." % i)

        rows.append({"index": i, "server": server, "name_raw": name_raw, "folder": folder,
                     "configured": configured, "mapping_present": mapping_present,
                     "remove": remove, "restore": restore, "server_folder": server_folder})

    if len(seen) != len(current_rows):
        raise ValueError("Every current category row must be posted exactly once.")

    counts = {}
    for row in current_rows:
        if row["configured"]:
            counts[row["name"]] = counts.get(row["name"], 0) + 1
    duplicate_names = {name for name, n in counts.items() if n > 1}

    # Removal safety: any removal checks references first, and malformed flow
    # JSON makes reference checking unreliable, so removals are refused.
    removed = set()
    for row in rows:
        if not row["remove"]:
            continue
        if row["server"] is None:
            continue
        name = row["server"]["name"]
        if invalid_flows:
            ids = ", ".join("#%s %s" % (f["id"], f["name"]) for f in invalid_flows)
            raise ValueError("Cannot remove %r while flow records are unreadable (%s). Repair them first." % (name, ids))
        labels = ["flow #%s “%s”" % (r["id"], r["name"])
                  for r in row["server"].get("flow_refs", [])]
        labels += ["classifier #%s “%s”" % (r["id"], r["name"])
                   for r in row["server"].get("classifier_refs", [])]
        if labels:
            raise ValueError("Cannot remove %r: still referenced by %s. Edit those first." % (name, ", ".join(labels)))
        removed.add(row["index"])

    # Names still held by a surviving current row, keyed by row identity: a
    # removed row frees only its own name (so remove-and-replace in one save is
    # allowed), while a case-variant sibling row still reserves its own name.
    removed_ids = {id(row["server"]) for row in rows
                   if row["index"] in removed and row["server"] is not None}
    reserved = {_name_key(r["name"]) for r in current_rows if id(r) not in removed_ids}

    categories = []
    restored = []
    mapping = {}
    for row in rows:
        server = row["server"]
        name = server["name"] if server is not None else row["name_raw"].strip()
        if server is None:
            if name == "" and row["folder"] == "":
                continue
            if name == "":
                raise ValueError("Row %d has a destination but no category name." % row["index"])
            if _has_control(name):
                raise ValueError("Row %d has control characters in its category name." % row["index"])
            if "," in name or "=" in name:
                raise ValueError("Row %d uses a comma or '=' in its category name; choose another." % row["index"])
            key = _name_key(name)
            if key in reserved:
                raise ValueError("Row %d duplicates an existing category name." % row["index"])
            reserved.add(key)
        matched_duplicate = server is not None and server["configured"] and name in duplicate_names
        if matched_duplicate and (row["remove"] or row["folder"] != row["server_folder"]):
            raise ValueError("Row %d is an exact duplicate category; edit it with a separate repair." % row["index"])
        if row["index"] in removed:
            continue
        if server is not None and server["configured"]:
            categories.append(name)
            if row["mapping_present"] or row["folder"] != "":
                mapping[name] = row["folder"]
        elif server is not None and not server["configured"]:
            if row["restore"]:
                restored.append(name)
            if row["mapping_present"] or row["folder"] != "":
                mapping[name] = row["folder"]
        else:  # new row
            categories.append(name)
            if row["folder"] != "":
                mapping[name] = row["folder"]

    result_categories = categories + restored
    if not result_categories and any(r["configured"] for r in current_rows):
        raise ValueError("Cannot remove every category; keep at least one.")
    return result_categories, mapping


EDITOR_PREVIEW_TMPL = """
<details class="card ux-preview" data-preview-kind="{{ kind }}" data-preview-id="{{ editor_id }}">
  <summary>Test this draft</summary>
  <p class="sub">Uses your unsaved edits in their list position. Rules run first; the first match wins. Nothing is saved, filed or sent. Disabled drafts are included for testing only.</p>
  <label>Use a recent message<select data-preview-message aria-label="Preview message"><option value="">Enter an example below…</option>{% for m in recent %}<option value="{{ m.id }}">{{ m.subject|clip(90) }} · {{ m.from_addr|clip(40) }}</option>{% endfor %}</select></label>
  <div class="grid2"><label>Sender<input type="text" data-preview-from placeholder="sender@example.com"></label><label>To<input type="text" data-preview-to placeholder="you@example.com"></label></div>
  <label>Subject<input type="text" data-preview-subject></label><label>Body<textarea data-preview-body rows="4"></textarea></label>
  <label class="check"><input type="checkbox" data-preview-llm> <span>Ask the classifier too · needed for AI category/topic filters and AI drafts</span></label>
  <button type="button" class="btn" data-preview-run>Run preview</button>
  <div class="ux-preview-output" role="status" aria-live="polite"></div>
</details>
"""

PREVIEW_REPORT_TMPL = """
<h3>What would happen</h3><p><b>{{ outcome }}</b></p>
<p class="sub">{{ name }} is tested at {{ placement }}. {{ 'Its conditions match this example.' if matched else 'Its conditions do not match this example (AI conditions need the classifier checkbox).' }}</p>
<p><b>This draft proposes:</b> {{ proposed|join(' → ') }}.</p>
<ul>{% for line in result.would %}<li>{{ line }}</li>{% endfor %}</ul>
{% if conditions %}<details open><summary>Condition results</summary><ul>{% for c in conditions %}<li>{{ c.text }} — <b>{{ c.result }}</b></li>{% endfor %}</ul></details>{% endif %}
{% if result.draft_preview %}{% set dp = result.draft_preview %}<h4>Draft preview</h4>{% if dp.body %}<pre>{{ dp.body }}</pre>{% elif dp.error %}<p>{{ dp.error }}</p>{% else %}<p>Ask the classifier too to generate this draft.</p>{% endif %}{% endif %}
{% for note in result.notes %}<p class="sub">{{ note }}</p>{% endfor %}
<p class="sub">Nothing was saved or applied.</p>
"""

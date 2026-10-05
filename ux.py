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

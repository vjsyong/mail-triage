# Flow builder v2: from form rows to a flow canvas (design record, 2026-10)

## The complaint
"the WHEN field just feels off... not intuitive... even the AI category thing feels
rigid... a graphical representation may be better? like xyflow."

## Research (2026-10-01)

**ReUI "React Flow blocks" (practitioner doctrine for node canvases):**
- The library is the easy part: nodes, edges, panning in an afternoon. The real work:
  nodes that read like the product (not like a diagram), edges a user can re-point,
  and menus that know what is safe to delete.
- Auto-layout (dagre, top-to-bottom) for flows whose order the user did not author;
  expose "reset layout". Keep an add-step affordance ON the connector
  ("cross-fades in when you hover or keyboard focus it, so a step lands exactly
  where the flow forks"). Editing affordances in 3 places: node toolbar, context
  menu, and the connector button.
- Anchors that cannot be deleted (a trigger). Inline validation, not toasts.
- Details open in an inspector; overlay on desktop, sheet on mobile.

**ui-patterns "Rule Builder":**
- Each rule = its own line/box, stacked vertically. All/any at the set level.
- SMART MINI-FORMS: the inputs vary by rule kind ("text search gets
  contains/does-not-contain; options get a picker"). A static field/op/value grid
  for every rule kind is the anti-pattern. This is exactly what felt "off".
- Add/remove buttons on each rule; the add button is how you choose the rule kind.

**workflowbuilder.io (node taxonomy):**
- Trigger nodes: light config, must sit at the start. Condition nodes: configure
  conditional logic so non-technical users need no code. Design the node library
  top-down from the user's mental model, not bottom-up from system capabilities.

**Market comparison (Intuz):** Zapier = linear guided builder, "extremely intuitive,
beginner-friendly"; Make/n8n = canvases, "visual clarity: trace exactly how data
flows" but steeper. Best of both for a LINEAR mail flow: a guided vertical SPINE
rendered with canvas conventions (nodes, handles, connectors, dot grid, insert
buttons) - the traceability of a graph with the direction of a sentence.

## Decisions (flow builder v2)
1. The editor is a single vertical flow canvas (dot grid background):
   TRIGGER node ("New mail arrives", fixed, undeletable) -> FILTER node
   ("Only when", match all/any) -> ACTION nodes chain, ending in an
   "Add a step" palette.
2. Conditions become smart mini-form chips whose fields CHANGE SHAPE per kind:
   plain text match shows field/operator/value; AI category shows a category
   picker (datalist of your categories) + min trust; about shows a description
   + min score. The kind is chosen by the ADD button ("+ match text",
   "+ AI category", "+ about (topic)") and can be switched per chip. The AI
   kinds are first-class citizens, not a buried dropdown - and they get the
   accent styling instead of looking like bolted-on form rows.
3. Steps are nodes: icon tile + title + one-line summary, click to expand and
   edit; toolbar (up/down/edit/remove) on each; "+" buttons live ON the
   connectors (hover/focus to reveal, always visible on touch), so a step can
   be inserted exactly where it belongs; the bottom palette appends.
4. A live plain-English summary bar sits above the canvas and updates as you
   edit ("new mail · from contains "x" AND ✦ about "y" -> move to Z, then LLM
   draft ..."). aria-live polite.
5. No free-form node dragging (the flow is linear; auto layout). Reorder via
   toolbar buttons - simpler, accessible, mobile-safe. Zero-radius theme kept;
   the xyflow feel comes from the grid, nodes, handles and connector inserts.
6. Mobile: same canvas, single column, insert buttons always visible, fields
   stack.

## Files
- `FLOW_EDIT_TMPL` fully rebuilt (self-contained styles + JS render/insert/summary).
- `_flow_edit_context` now serves categories_json / watch_text / poll_interval /
  summary_text / is_new; all four call sites (new GET+POST-error, edit GET+POST-error)
  go through it.
- POST contract unchanged (cond_kind_i / cond_field_i / cond_op_i / cond_value_i /
  cond_score_i + steps_json) - the new UI is presentation + interaction.

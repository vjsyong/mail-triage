# Plugins UI: research + redesign

Status: applied 2026-10-02. Supersedes the card-stack layout from the first plugin
wave (every plugin's grants, assistant gate and settings dumped inline). Applies the
pattern set below to `/plugins` (list) and the new `/plugins/<id>` (detail page).

## Sources

- NN/g, *Progressive Disclosure* (nngroup.com/articles/progressive-disclosure):
  initial view shows only the few most important options; specialized options move
  to a secondary level, reached through a control with "strong information scent".
  Two disclosure levels work; more get lost.
- NN/g, *Toggle-Switch Guidelines* (nngroup.com/articles/toggle-switch-guidelines):
  toggles are for two mutually exclusive states with a default and immediate effect;
  label with the keyword first, no questions; deliver the result immediately.
- VS Code, *Extension Marketplace* docs (code.visualstudio.com/docs/configure/
  extensions/extension-marketplace): the Extensions view is a flat list where each
  item carries a brief description and its own manage control; enabling/disabling is
  a per-item action, disabled items stay in the list marked Disabled; selecting an
  item opens a details page (README, what it contributes, its settings, changelog);
  per-extension configuration lives with the extension, not in a global dump.
- Chrome extension management (chrome://extensions, and the Web Store install
  prompt): a card/row per extension with a single on/off toggle and a Details
  button; permissions are presented in plain language as a short consent list
  ("Read and change your data on ..."), not as raw scope names.
- Home Assistant integrations dashboard: list of installed integrations; each opens
  a dialog with configure/disable, its entities and its log; state is visible at a
  glance.

## Pattern set (distilled)

1. One row per plugin: icon, name, version, one-line description, kind badge, a
   single on/off toggle, and a chevron into details. The list answers exactly one
   question: what is installed and what is running.
2. State is consistent and immediate: the same toggle in the same place on every
   row; no separate "Enable"/"Disable" buttons; no "disabled" badge versus prose in
   two different spots.
3. Depth goes to the detail page: what it does, what it can access, who can call it,
   its settings, its activity. Nothing advanced sits on the list.
4. Consent in human terms: each capability gets a plain-language title and a one
   line "why", with the raw scope name kept as secondary mono text; a plugin with
   no host access says so in one line.
5. Grouping beats sorting: "Running" above "Off" is enough for a single-user
   install; no filters until the list actually grows.
6. The opening view is small: a one-line description under the title; developer
   scaffolding (paths, SDK, docs) folds away at the bottom.
7. Configuration lives on the thing it configures (per-plugin settings form on the
   detail page, mirroring VS Code/Obsidian practice).
8. Immediate-effect controls never hide behind confirmations; destructive ones do
   not exist in v1 (disable is the toggle; nothing uninstalls).

## What moved

- Enable/Disable buttons -> one toggle per row (list) and one in the detail header.
- Grants checkboxes, assistant permission, pipeline opt-ins, settings fields ->
  detail page (`/plugins/<id>`), grouped as Access / Settings / Activity.
- Capability names -> human titles + explanations (raw name kept as mono).
- The intro prose paragraph (sandbox model, paths, SDK, docs) -> one-line subtitle
  + a "Developer details" fold at the bottom of each page.
- New: classifier plugins get their "Pipeline use" opt-in surfaced (previously the
  `plugin_classifiers` list had no UI at all, so an enabled classifier plugin
  silently did nothing).

## Not done (deliberately)

- Search/filter (7 plugins; add @installed-style filters when the catalogue grows).
- Marketplace/browse tab (no remote catalogue exists; user plugins are folders).
- Uninstall (built-ins ship with the app; user plugins are removed from disk).

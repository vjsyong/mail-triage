// plugin-sdk.d.ts - Mail Triage plugin SDK v0.1 (synchronous)
//
// The SDK is intentionally tiny: everything a plugin can touch is a host
// function listed here. There is no filesystem, no process, no network beyond
// ctx.http, and no way to reach the mailbox except ctx.mail (read) and
// ctx.action (propose). Plugins never mutate mail directly; they propose
// actions the kernel executes only after the user clicks.
//
// Runtime contract (SDK v0.1):
//   - bundle format: a single .js file (esbuild: --format=iife --global-name=__mt_plugin)
//     evaluated after the host bootstrap; it must assign globalThis.__mt_plugin = { onLoad, execute, onUnload }
//   - ALL calls are synchronous. Declaring an `async` function here will throw
//     "must be synchronous" at call time. Host calls block, with the manifest's
//     limits.timeout_ms enforced by the interpreter.

export interface MessageRef {
  id: number;            // local index id (stable per message row)
  folder: string;
  uid: number;
  subject: string;
  from: string;
  date: string;          // RFC822 header string
  snippet: string;
  category?: string | null;   // LLM category when classified
  needs_reply?: boolean;
}

export interface Message extends MessageRef {
  to: string;
  body_text: string;
  category: string | null;
  tags: string[];
  needs_reply: boolean;
}

export interface ToolCall {
  tool: string;          // tool name as declared in the manifest
  args: Record<string, unknown>;   // validated against the declared parameters schema
  deadline_ms: number;   // remaining wall clock for this call
}

export interface ToolResult {
  ok: boolean;
  summary: string;       // one line for the chat transcript
  data?: unknown;        // machine-readable payload for the model
  card?: ActionCard;     // optional rich card in the UI
  error?: { code: "invalid_args" | "denied" | "timeout" | "quota" | "internal";
            message: string };
}

export interface CardAction {
  id: string;
  label: string;         // e.g. "Apply", "Archive", "Open"
  kind: "apply" | "dismiss" | "link";   // "apply" returns control to the kernel
  url?: string;          // only for kind: "link"
}

export interface ActionCard {
  title: string;
  markdown?: string;     // rendered with the app's markdown renderer
  fields?: Array<{ label: string; value: string }>;
  actions: CardAction[]; // rendered like the existing proposal cards
}

export interface PluginKV {
  get<T = unknown>(key: string): T | undefined;
  set(key: string, value: unknown): void;    // JSON; quota from limits.kv_bytes
  delete(key: string): void;
  list(prefix?: string): string[];
}

export interface PluginMail {
  // requires permission: mailbox.read
  search(q: { query?: string; sender?: string; subject?: string;
              folder?: string; limit?: number; since_days?: number }): MessageRef[];
  read(id: number): Message;
}

export interface PluginLLM {
  // requires permission: llm.complete / llm.embed
  complete(req: { prompt: string; system?: string; max_tokens?: number;
                  json?: boolean }): { text: string };
  embed(texts: string[]): number[][];
}

export interface HttpResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}

export interface PluginContext {
  readonly plugin: { id: string; version: string };

  log(level: "debug" | "info" | "warn" | "error", message: string): void;

  config: { get(): Record<string, unknown> };   // validated against manifest.config

  kv: PluginKV;
  mail: PluginMail;
  llm: PluginLLM;

  http: {
    // requires permission: net.http; the URL host must match manifest.net.hosts
    fetch(url: string, init?: { method?: "GET" | "POST";
                                headers?: Record<string, string>;
                                body?: string; timeout_ms?: number }): HttpResponse;
  };

  action: {
    // Propose a user-visible action. The kernel stores it as a pending action
    // and renders an Action Card; executing it is a kernel code path the user
    // triggers with a click.  (Wired in Phase 4; call available from the start.)
    propose(card: ActionCard): { action_id: string };
  };
}

/** Called once when the plugin is loaded into the runtime. */
export function onLoad(ctx: PluginContext): void;

/** Called for every tool invocation routed to this plugin. */
export function execute(ctx: PluginContext, call: ToolCall): ToolResult;

/** Called before unload/disable/reload. Best-effort; must not throw. */
export function onUnload(ctx: PluginContext): void;

/**
 * Classifier kind: mirror a native heuristic's model. Wired from
 * heuristics.classify() when the plugin is opted in via
 * settings.plugin_classifiers ([{plugin, heuristic_id}]); the mirrored
 * heuristic is usually parked disabled - the plugin stands in for its
 * predictions. The host feeds the heuristic's kind + model and the featurized
 * message ({tokens, from, domain, subject, body}). Return label/confidence,
 * or null to abstain.
 */
export function classify(ctx: PluginContext,
                         input: { kind: string; model: unknown; feats: unknown }):
  { label: string; confidence: number; detail?: string | null } | null;

/**
 * Matcher kind: a rule/flow condition the native ops cannot express.
 * Referenced from conditions as {"field":"subject","op":"plugin","plugin":"<id>"};
 * gated by the "Pipeline use" opt-in on the Plugins page. Keep it fast - it
 * runs per message in the scan.
 */
export function match(ctx: PluginContext,
                      input: { fields: Record<string, string>; text: string }):
  boolean | { match: boolean; detail?: string };

/**
 * Draft-provider kind: the reply body a flow's draft step would save. Flow
 * steps reference it as {"type":"draft","mode":"plugin","plugin":"<id>"}.
 * May use ctx.llm.complete; budget limits.timeout_ms accordingly (LLM calls
 * count against the deadline).
 */
export function draft(ctx: PluginContext,
                      input: { subject: string; from: string; snippet: string;
                               instructions?: string }):
  { text: string } | string;

/**
 * Retriever kind: re-rank semantic-search candidates. Opt-in via the
 * "Pipeline use" checkbox (plugin_retrievers setting; first opted-in plugin
 * wins). Return the ids in your preferred order; unknown ids are ignored and
 * unranked candidates keep their incoming order after yours.
 */
export function rank(ctx: PluginContext,
                     input: { query: string;
                              candidates: Array<{ id: number; subject: string; from: string;
                                                  snippet: string; tags: string[];
                                                  needs_reply: boolean }> }):
  { ids: number[] } | number[];

/**
 * Integration kind: react to kernel events (mail.filed, mail.classified).
 * Delivered on a background queue - never blocks the mail pipeline; give the
 * handler a short limits.timeout_ms. Network hosts must be listed in
 * manifest.net.hosts or - with net.allow_config_hosts: true - in the config's
 * allowed_hosts list (the user typed the endpoint; every call is audited).
 */
export function onEvent(ctx: PluginContext,
                        event: { type: string; payload: Record<string, unknown>; ts: number }):
  void | { sent?: boolean; reason?: string };

/**
 * Scheduled entrypoint: the kernel invokes this while the plugin is enabled and
 * its manifest declares `schedule.every_minutes`. Runs on a background thread
 * (never the mail pipeline), so keep it within limits.timeout_ms. Return a
 * ToolResult to report, or call ctx.action.propose(card) to surface a pending
 * card to the user. `input.last_run` is the previous run's unix seconds (0 on
 * the first), `input.now` the current unix seconds, `input.run_tool` the
 * manifest's hint (or the first tool).
 */
export function onSchedule(ctx: PluginContext,
                           input: { every_minutes: number; last_run: number;
                                    now: number; run_tool: string }):
  void | ToolResult;

// ---- optional browser pages (`manifest.ui`) -------------------------------
//
// A plugin may ship one browser bundle (`ui.entrypoint`) served by the host at
// /extensions/<id>/<page>. It runs in a sandboxed, opaque-origin frame (no
// same-origin, no network) and reaches the backend only through the explicit
// `ui.operations` allowlist (declared tools that are read-only). See
// docs/plugin-pages.md. The browser bundle registers renderers on
// globalThis.__mt_ui; the host injects sdk/ui.js (the MTUI page SDK) first.

export interface UiManifest {
  entrypoint: string;                    // browser bundle, relative + .js
  pages: Array<{ id: string; title: string; description?: string }>;
  navigation?: Array<{
    page: string;                        // must reference a declared page id
    label: string;
    group?: "mail" | "automation" | "system";
    icon?: "mail" | "inbox" | "search" | "list" | "tag" | "star" | "clock" |
           "filter" | "file" | "puzzle" | "sparkles" | "settings";
    order?: number;
  }>;
  operations?: string[];                 // declared read-only tool names, explicit allowlist
}

export type UiErrorCode = "invalid_args" | "denied" | "forbidden" | "timeout" |
                          "quota" | "disabled" | "not_found" | "internal";

export interface UiResult {
  ok: boolean;
  data?: Record<string, unknown>;
  summary?: string;
  error?: { code: UiErrorCode; message: string };
}

export interface PageApi {
  /** Call one allowlisted backend operation through the host bridge. */
  call(op: string, args?: Record<string, unknown>): Promise<UiResult>;
  /** Ask the host to update the URL (bounded q/message state only). */
  updateUrl(next: { q?: string; message?: string }, replace?: boolean): void;
  setStatus(text: string): void;
  log(message: string): void;
  isDisposed(): boolean;
  on(kind: "theme" | "state" | "dispose", fn: (value: unknown) => void): () => void;
  getState(): { q?: string; message?: string };
  components: PageComponents;
}

export interface PageComponents {
  searchField(opts: { value?: string; placeholder?: string; ariaLabel?: string;
                      buttonLabel?: string; onSearch?: (q: string) => void;
                      onInput?: (q: string) => void }): HTMLFormElement;
  splitPane(opts?: { start?: Node; end?: Node }): HTMLElement & {
    showList(): void; showReader(): void };
  messageList(opts: { items: Array<Record<string, unknown>>; selected?: number;
                      onSelect?: (id: number, item: unknown) => void }): HTMLElement;
  plainTextReader(opts: { message?: null | Record<string, unknown> }): HTMLElement;
  stateView(kind: "loading" | "empty" | "error" | "denied",
            opts?: { message?: string; retry?: () => void; retryLabel?: string }): HTMLElement;
  el(tag: string, cls?: string | null, text?: string): HTMLElement;
}

export interface PageBundle {
  pages: Record<string, { render(root: HTMLElement, api: PageApi): void }>;
}

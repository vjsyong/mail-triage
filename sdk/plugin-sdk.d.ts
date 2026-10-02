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
              folder?: string; limit?: number }): MessageRef[];
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

/* Mail Triage plugin runtime bootstrap (SDK v0.1).
 *
 * The host evaluates, in the same QuickJS context:
 *   1. this file
 *   2. the plugin bundle (must assign globalThis.__mt_plugin = {onLoad, execute, onUnload})
 *   3. __mt_boot()
 *
 * It exposes:
 *   - __host(name, payloadJson) -> resultJson   (injected by the host; the only way out)
 *   - __mt_ctx   the PluginContext implementation wired to __host
 *   - __mt_boot / __mt_exec / __mt_unload       (called by the host)
 *
 * All calls are synchronous. Host errors arrive as {"__error": {code, message}}.
 */
(function () {
  if (typeof __host !== "function") {
    throw new Error("mail-triage plugin runtime: host bridge missing");
  }

  function hostCall(name, payload) {
    var out = __host(name, JSON.stringify(payload === undefined ? {} : payload));
    var res = JSON.parse(out);
    if (res && res.__error) {
      var err = new Error(res.__error.message || "host error");
      err.code = res.__error.code || "internal";
      throw err;
    }
    return res;
  }

  function buildCtx() {
    var info = hostCall("plugin.info", {});
    return {
      plugin: { id: info.id, version: info.version },
      log: function (level, message) {
        __host("log", JSON.stringify({ level: String(level), message: String(message) }));
      },
      config: { get: function () { return hostCall("config.get", {}); } },
      kv: {
        get: function (k) { return hostCall("kv.get", { key: String(k) }); },
        set: function (k, v) { return hostCall("kv.set", { key: String(k), value: v }); },
        delete: function (k) { return hostCall("kv.delete", { key: String(k) }); },
        list: function (prefix) { return hostCall("kv.list", { prefix: String(prefix || "") }); }
      },
      mail: {
        search: function (q) { return hostCall("mail.search", q || {}); },
        read: function (id) { return hostCall("mail.read", { id: id }); }
      },
      llm: {
        complete: function (req) { return hostCall("llm.complete", req || {}); },
        embed: function (texts) { return hostCall("llm.embed", { texts: texts }); }
      },
      http: {
        fetch: function (url, init) {
          return hostCall("http.fetch", { url: String(url), init: init || {} });
        }
      },
      action: {
        propose: function (card) { return hostCall("action.propose", { card: card }); }
      }
    };
  }

  var _native = (typeof console !== "undefined") ? console : null;
  function logArgs(args) {
    var parts = [];
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      parts.push(typeof a === "string" ? a : JSON.stringify(a));
    }
    return parts.join(" ");
  }
  if (_native) {
    _native.log = function () {
      __host("log", JSON.stringify({ level: "info", message: logArgs(arguments) }));
    };
    _native.warn = function () {
      __host("log", JSON.stringify({ level: "warn", message: logArgs(arguments) }));
    };
    _native.error = function () {
      __host("log", JSON.stringify({ level: "error", message: logArgs(arguments) }));
    };
  }

  globalThis.__mt_boot = function () {
    globalThis.__mt_ctx = buildCtx();
    var p = globalThis.__mt_plugin;
    if (!p || typeof p !== "object") {
      throw new Error("bundle did not assign __mt_plugin = {onLoad, execute, onUnload}");
    }
    if (typeof p.onLoad === "function") {
      var r = p.onLoad(globalThis.__mt_ctx);
      if (r && typeof r.then === "function") {
        throw new Error("onLoad must be synchronous (SDK v0.1)");
      }
    }
    return "ok";
  };

  globalThis.__mt_exec = function (callJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.execute !== "function") {
      throw new Error("bundle does not export execute()");
    }
    var call = JSON.parse(callJson);
    var result = p.execute(globalThis.__mt_ctx, call);
    if (result && typeof result.then === "function") {
      throw new Error("execute must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(result === undefined ? null : result);
  };

  globalThis.__mt_classify = function (inputJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.classify !== "function") {
      throw new Error("bundle does not export classify()");
    }
    var out = p.classify(globalThis.__mt_ctx, JSON.parse(inputJson));
    if (out && typeof out.then === "function") {
      throw new Error("classify must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  globalThis.__mt_match = function (inputJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.match !== "function") {
      throw new Error("bundle does not export match()");
    }
    var out = p.match(globalThis.__mt_ctx, JSON.parse(inputJson));
    if (out && typeof out.then === "function") {
      throw new Error("match must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  globalThis.__mt_draft = function (inputJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.draft !== "function") {
      throw new Error("bundle does not export draft()");
    }
    var out = p.draft(globalThis.__mt_ctx, JSON.parse(inputJson));
    if (out && typeof out.then === "function") {
      throw new Error("draft must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  globalThis.__mt_rank = function (inputJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.rank !== "function") {
      throw new Error("bundle does not export rank()");
    }
    var out = p.rank(globalThis.__mt_ctx, JSON.parse(inputJson));
    if (out && typeof out.then === "function") {
      throw new Error("rank must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  globalThis.__mt_event = function (eventJson) {
    var p = globalThis.__mt_plugin;
    if (!p || typeof p.onEvent !== "function") {
      throw new Error("bundle does not export onEvent()");
    }
    var out = p.onEvent(globalThis.__mt_ctx, JSON.parse(eventJson));
    if (out && typeof out.then === "function") {
      throw new Error("onEvent must be synchronous (SDK v0.1)");
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  globalThis.__mt_unload = function () {
    var p = globalThis.__mt_plugin;
    if (p && typeof p.onUnload === "function") {
      p.onUnload(globalThis.__mt_ctx);
    }
    return "ok";
  };
})();

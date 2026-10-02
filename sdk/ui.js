/* Mail Triage browser UI SDK (page SDK v1).
 *
 * The host embeds this file (then the plugin's own `ui.entrypoint` bundle, then
 * a start call) inside a sandboxed, opaque-origin iframe with a nonce CSP that
 * denies network, forms, frames and workers. This file is the ONLY bridge the
 * page code gets: it talks to the host page over postMessage and exposes:
 *
 *   MTUI.call(op, args) -> Promise<{ok, data, error, summary}>
 *       Call one operation declared in the manifest's ui.operations allowlist.
 *       The server re-checks the view session, the plugin's enable state and
 *       the allowlist, then runs the tool through the normal sandbox runtime.
 *
 *   MTUI.updateUrl({q, message}, replace)
 *       Ask the host to update the page URL (bounded q/message state only).
 *
 *   MTUI.on('theme' | 'state' | 'dispose', fn)
 *       Theme tokens / host URL state / teardown notifications.
 *
 *   MTUI.components.*
 *       Rendering-only helpers (search field, split pane, message list,
 *       plain-text reader, loading/empty/error states). They never fetch.
 *
 * Page bundles register their renderers on globalThis.__mt_ui:
 *   globalThis.__mt_ui = { pages: { desk: { render: function(root, api){} } } };
 */
(function () {
  "use strict";

  var boot = window.__MT_BOOT || {};
  var bridge = null;  // {port, sid} set only by the host-gated bootstrap
  try {
    // Layout mode comes from the host (the frame is often much narrower than
    // the device, so the page's own media queries cannot decide this).
    document.documentElement.setAttribute("data-mt-layout", "desktop");
  } catch (e) { /* ignore */ }
  var listeners = { theme: [], state: [], dispose: [] };
  var theme = {};
  var state = boot.state || {};
  var disposed = false;
  var seq = 0;
  var waiting = {};

  function send(msg) {
    try {
      if (!bridge || !bridge.port) return;
      var out = { __mt: 1, sid: bridge.sid };
      for (var k in msg) {
        if (Object.prototype.hasOwnProperty.call(msg, k)) out[k] = msg[k];
      }
      bridge.port.postMessage(out);
    } catch (e) { /* frame gone */ }
  }

  function errFrom(envelope) {
    var e = (envelope && envelope.error) || {};
    var err = new Error(e.message || "call failed");
    err.code = e.code || "internal";
    return err;
  }

  function call(op, args) {
    if (disposed) return Promise.reject(new Error("disposed"));
    var id = "c" + (++seq);
    return new Promise(function (resolve, reject) {
      waiting[id] = { resolve: resolve, reject: reject };
      send({ kind: "call", id: id, op: String(op), args: args || {} });
      setTimeout(function () {
        if (waiting[id]) {
          delete waiting[id];
          var e = new Error("timed out");
          e.code = "timeout";
          reject(e);
        }
      }, 21000);
    });
  }

  function updateUrl(next, replace) {
    next = next || {};
    send({ kind: "nav", q: next.q, message: next.message, replace: !!replace });
  }

  function setStatus(text) {
    send({ kind: "status", text: String(text == null ? "" : text).slice(0, 120) });
  }

  function log(msg) {
    send({ kind: "log", message: String(msg == null ? "" : msg).slice(0, 500) });
  }

  function on(kind, fn) {
    if (!listeners[kind]) return function () {};
    listeners[kind].push(fn);
    return function () {
      var i = listeners[kind].indexOf(fn);
      if (i >= 0) listeners[kind].splice(i, 1);
    };
  }

  function emit(kind, val) {
    (listeners[kind] || []).slice().forEach(function (fn) {
      try { fn(val); } catch (e) { /* page error must not break the bridge */ }
    });
  }

  function getState() { return state; }
  function getTheme() { return theme; }
  function isDisposed() { return disposed; }

  function applyTheme() {
    try {
      var root = document.documentElement;
      for (var k in theme) {
        if (k.indexOf("--") === 0 && theme[k]) root.style.setProperty(k, theme[k]);
      }
    } catch (e) { /* ignore */ }
  }

  function disposeLocal() {
    if (disposed) return;
    disposed = true;
    for (var id in waiting) {
      if (waiting[id]) { waiting[id].reject(new Error("disposed")); delete waiting[id]; }
    }
    emit("dispose");
    // Clear any rendered mail text so nothing sensitive lingers after teardown.
    try {
      var root = document.getElementById("mt-root");
      if (root) root.textContent = "";
    } catch (e) { /* ignore */ }
  }

  function handle(d) {
    if (!d || d.__mt !== 1) return;
    if (bridge && bridge.sid && d.sid && d.sid !== bridge.sid) return;
    if (d.kind === "theme") {
      theme = d.theme || {};
      applyTheme();
      emit("theme", theme);
    } else if (d.kind === "state") {
      state = d.state || {};
      emit("state", state);
    } else if (d.kind === "layout") {
      try {
        document.documentElement.setAttribute("data-mt-layout",
          d.layout === "mobile" ? "mobile" : "desktop");
      } catch (e) { /* ignore */ }
    } else if (d.kind === "result") {
      var w = waiting[d.id];
      if (w) {
        delete waiting[d.id];
        if (d.ok) w.resolve(d); else w.reject(errFrom(d));
      }
    } else if (d.kind === "ping") {
      send({ kind: "pong" });
    } else if (d.kind === "dispose") {
      disposeLocal();
    }
  }

  // The host-gated bootstrap calls this ONLY after it transferred a MessagePort
  // tied to THIS document. Operational messages then travel on that port, never
  // the global WindowProxy, so a remote page that later navigates the frame
  // cannot inherit or re-establish the bridge (AR2-3).
  window.__mt_sdk_connect = function (port, sid, b) {
    if (bridge) return;
    if (b) { boot = b; window.__MT_BOOT = b; }
    bridge = { port: port, sid: sid || boot.sid || "" };
    state = boot.state || state || {};
    try {
      port.onmessage = function (e) { handle(e.data); };
      if (port.start) port.start();
    } catch (e) { /* ignore */ }
    send({ kind: "ready" });
  };

  /* ---------------------------------------------------------------- elements */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  function searchField(opts) {
    opts = opts || {};
    var form = el("form", "mt-search");
    form.setAttribute("role", "search");
    var input = el("input");
    input.type = "search";
    input.className = "mt-search-input";
    input.placeholder = opts.placeholder || "Search";
    input.value = opts.value || "";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("aria-label", opts.ariaLabel || "Search");
    var btn = el("button", "mt-btn", opts.buttonLabel || "Search");
    btn.type = "button";
    form.appendChild(input);
    form.appendChild(btn);
    function fire() { if (opts.onSearch) opts.onSearch(input.value); }
    // The frame denies native form submission (sandbox + form-action 'none'),
    // so drive search from the button, Enter and the (prevented) submit event.
    btn.addEventListener("click", fire);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); fire(); }
    });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      fire();
    });
    input.addEventListener("input", function () {
      if (opts.onInput) opts.onInput(input.value);
    });
    form.input = input;
    return form;
  }

  function splitPane(opts) {
    opts = opts || {};
    var wrap = el("div", "mt-split");
    wrap.setAttribute("data-view", "list");
    var start = el("div", "mt-pane-list");
    var end = el("div", "mt-pane-reader");
    if (opts.start) start.appendChild(opts.start);
    if (opts.end) end.appendChild(opts.end);
    wrap.appendChild(start);
    wrap.appendChild(end);
    wrap.showList = function () { wrap.setAttribute("data-view", "list"); };
    wrap.showReader = function () { wrap.setAttribute("data-view", "reader"); };
    return wrap;
  }

  function messageList(opts) {
    opts = opts || {};
    var list = el("div", "mt-msgs");
    list.setAttribute("role", "listbox");
    (opts.items || []).forEach(function (it) {
      var row = el("div", "mt-msg");
      row.setAttribute("role", "option");
      row.tabIndex = 0;
      var selected = it.id === opts.selected;
      row.setAttribute("aria-selected", String(selected));
      if (selected) row.classList.add("on");
      var top = el("div", "mt-msg-top");
      top.appendChild(el("span", "mt-msg-from", it.from || ""));
      top.appendChild(el("span", "mt-msg-when", it.date || ""));
      row.appendChild(top);
      row.appendChild(el("div", "mt-msg-subject", it.subject || "(no subject)"));
      if (it.snippet) row.appendChild(el("div", "mt-msg-snippet", it.snippet));
      function choose() { if (opts.onSelect) opts.onSelect(it.id, it); }
      row.addEventListener("click", choose);
      row.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choose(); }
      });
      list.appendChild(row);
    });
    return list;
  }

  function plainTextReader(opts) {
    opts = opts || {};
    var box = el("div", "mt-reader");
    var m = opts.message;
    if (!m) {
      box.appendChild(el("div", "mt-empty", "Select a message to read."));
      return box;
    }
    var head = el("div", "mt-reader-head");
    head.appendChild(el("h2", "mt-reader-subject", m.subject || "(no subject)"));
    var meta = el("div", "mt-reader-meta");
    [["From", m.from], ["To", m.to], ["Date", m.date]].forEach(function (r) {
      if (!r[1]) return;
      var line = el("div");
      line.appendChild(el("b", null, r[0] + ": "));
      line.appendChild(document.createTextNode(String(r[1])));
      meta.appendChild(line);
    });
    var chips = el("div", "mt-chips");
    if (m.category) chips.appendChild(el("span", "mt-chip", m.category));
    (m.tags || []).forEach(function (t) { chips.appendChild(el("span", "mt-chip", "#" + t)); });
    if (m.needs_reply) chips.appendChild(el("span", "mt-chip warn", "needs reply"));
    if (chips.childNodes.length) meta.appendChild(chips);
    head.appendChild(meta);
    box.appendChild(head);
    box.appendChild(el("pre", "mt-reader-body", m.body_text || ""));
    box.appendChild(el("div", "mt-hint",
      "Local-index text only - attachments and rich mail HTML are not shown."));
    return box;
  }

  function stateView(kind, opts) {
    opts = opts || {};
    var box = el("div", "mt-state mt-state-" + kind);
    box.setAttribute("role", kind === "error" ? "alert" : "status");
    box.appendChild(el("div", "mt-state-msg", opts.message || ""));
    if (opts.retry) {
      var b = el("button", "mt-btn", opts.retryLabel || "Retry");
      b.type = "button";
      b.addEventListener("click", opts.retry);
      box.appendChild(b);
    }
    return box;
  }

  function start() {
    var pages = (window.__mt_ui && window.__mt_ui.pages) || {};
    var page = pages[boot.page];
    var root = document.getElementById("mt-root");
    if (!root) return;
    root.textContent = "";
    if (!page || typeof page.render !== "function") {
      root.appendChild(stateView("error", { message: "This page has no renderer." }));
      return;
    }
    var api = {
      call: call, updateUrl: updateUrl, setStatus: setStatus, log: log,
      on: on, getState: getState, getTheme: getTheme, isDisposed: isDisposed,
      components: {
        searchField: searchField, splitPane: splitPane, messageList: messageList,
        plainTextReader: plainTextReader, stateView: stateView, el: el
      }
    };
    try {
      page.render(root, api);
    } catch (e) {
      root.textContent = "";
      root.appendChild(stateView("error", { message: "The page failed to render." }));
    }
  }

  window.MTUI = {
    start: start, call: call, updateUrl: updateUrl, setStatus: setStatus,
    getState: getState, getTheme: getTheme, on: on,
    components: {
      searchField: searchField, splitPane: splitPane, messageList: messageList,
      plainTextReader: plainTextReader, stateView: stateView, el: el
    }
  };

})();

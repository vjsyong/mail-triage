/* Fusion Lab browser page (trusted tier).
 *
 * Host-rendered chrome comes from the base stylesheet; this bundle only builds
 * DOM with textContent (no raw HTML) and talks to the backend through
 * api.call(op, args) for the declared read-only operations.
 */
(function () {
  "use strict";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  function chip(text) { return el("span", "mt-chip", text); }

  function renderResult(root, api) {
    root.textContent = "";

    var search = el("form", "mt-search");
    search.setAttribute("role", "search");
    var input = el("input");
    input.type = "search";
    input.className = "mt-search-input";
    input.placeholder = "Filter by subject or sender…";
    input.setAttribute("autocomplete", "off");
    var go = el("button", "mt-btn", "Refresh");
    go.type = "submit";
    search.appendChild(input);
    search.appendChild(go);
    root.appendChild(search);

    var split = el("div", "mt-split");
    split.setAttribute("data-view", "list");
    var listPane = el("div", "mt-pane-list");
    var reader = el("div", "mt-pane-reader");
    split.appendChild(listPane);
    split.appendChild(reader);
    root.appendChild(split);

    var back = el("button", "mt-btn mt-back", "← Back");
    back.type = "button";
    back.addEventListener("click", function () { split.setAttribute("data-view", "list"); });
    reader.appendChild(back);

    var detail = el("div", "mt-reader");
    reader.appendChild(detail);

    function load() {
      listPane.textContent = "";
      listPane.appendChild(el("div", "mt-empty", "Loading…"));
      api.call("list_messages", { query: input.value, limit: 30 })
        .then(function (res) {
          listPane.textContent = "";
          var msgs = ((res.data || {}).messages) || [];
          if (!msgs.length) {
            listPane.appendChild(el("div", "mt-empty", "No indexed messages."));
            return;
          }
          msgs.forEach(function (m) {
            var row = el("div", "mt-msg");
            var top = el("div", "mt-msg-top");
            top.appendChild(el("div", "mt-msg-from", m.from || "(no sender)"));
            top.appendChild(el("div", "mt-msg-when", (m.date || "").slice(0, 16)));
            row.appendChild(top);
            row.appendChild(el("div", "mt-msg-subject", m.subject || "(no subject)"));
            var meta = el("div", "mt-msg-snippet");
            meta.textContent = (m.category || "unclassified") +
              (m.needs_reply ? " · needs reply" : "");
            row.appendChild(meta);
            row.addEventListener("click", function () {
              var on = listPane.querySelector(".mt-msg.on");
              if (on) on.classList.remove("on");
              row.classList.add("on");
              split.setAttribute("data-view", "reader");
              compare(m);
            });
            listPane.appendChild(row);
          });
        })
        .catch(function (err) {
          listPane.textContent = "";
          listPane.appendChild(el("div", "mt-state", "Could not load messages: " +
            ((err && err.message) || "error")));
        });
    }

    function verdictBlock(title, v, extra) {
      var box = el("div");
      box.style.marginBottom = "14px";
      box.appendChild(el("div", "mt-msg-from", title));
      var chips = el("div", "mt-chips");
      var verdict = (v && v.verdict) || null;
      if (v && v.ok && verdict) {
        chips.appendChild(chip("category: " + (verdict.category || "?")));
        if (verdict.needs_reply !== undefined && verdict.needs_reply !== null) {
          chips.appendChild(chip("needs reply: " + (verdict.needs_reply ? "yes" : "no")));
        }
        if (verdict.confidence !== undefined && verdict.confidence !== null) {
          chips.appendChild(chip("confidence: " + verdict.confidence));
        }
        if (verdict.needs_reply_confidence !== undefined &&
            verdict.needs_reply_confidence !== null) {
          chips.appendChild(chip("nr confidence: " + verdict.needs_reply_confidence));
        }
        chips.appendChild(chip("latency: " + v.ms + " ms"));
        if (extra) chips.appendChild(chip(extra));
      } else {
        chips.appendChild(chip(v && v.ok ? "unsupported shape" : "not available"));
      }
      box.appendChild(chips);
      if (v && v.ok && verdict && (verdict.summary || verdict.reason)) {
        box.appendChild(el("div", "mt-hint", verdict.summary || ""));
        box.appendChild(el("div", "mt-hint", verdict.reason || ""));
      }
      if (v && v.error) {
        box.appendChild(el("div", "mt-hint", "error: " + v.error));
      }
      return box;
    }

    function compare(m) {
      detail.textContent = "";
      detail.appendChild(el("h2", "mt-reader-subject", m.subject || "(no subject)"));
      var meta = el("div", "mt-reader-meta");
      meta.appendChild(el("div", null, (m.from || "") + " · " + (m.date || "")));
      meta.appendChild(el("div", null, "stored verdict: " + (m.category || "unclassified") +
        (m.needs_reply ? " · needs reply" : "")));
      detail.appendChild(meta);
      detail.appendChild(el("div", "mt-state", "Running primary, fallback and fusion…"));

      api.call("compare_message", { message_id: m.id })
        .then(function (res) {
          var d = res.data || {};
          detail.textContent = "";
          detail.appendChild(back);
          detail.appendChild(el("h2", "mt-reader-subject", m.subject || "(no subject)"));
          var meta2 = el("div", "mt-reader-meta");
          meta2.appendChild(el("div", null, (d.message.from || "") +
            (d.message.to ? " → " + d.message.to : "")));
          meta2.appendChild(el("div", null, (d.message.date || "") + " · stored: " +
            (d.message.category || "unclassified") +
            (d.message.needs_reply ? " · needs reply" : "")));
          detail.appendChild(meta2);

          detail.appendChild(verdictBlock("Primary LLM", d.primary, null));
          detail.appendChild(verdictBlock("Fallback LLM", d.fallback, null));

          var comp = ((d.fusion || {}).verdict || {}).components || {};
          var extra = comp.tinyjev_ms !== undefined ? "TinyJev " + comp.tinyjev_ms + " ms"
                                                     : null;
          detail.appendChild(verdictBlock("MiniCPM5-2B-TinyJev-Fusion", d.fusion, extra));
          detail.appendChild(el("div", "mt-hint",
            "Primary and fallback answer the production classify prompt. " +
            "Fusion = TinyJev category + MiniCPM direct needs_reply prompt (recall wording, " +
            "thinking off). Compare against the stored verdict, then decide."));
        })
        .catch(function (err) {
          detail.textContent = "";
          detail.appendChild(back);
          detail.appendChild(el("div", "mt-state", "Comparison failed: " +
            ((err && err.message) || "error")));
        });
    }

    search.addEventListener("submit", function (e) {
      e.preventDefault();
      load();
    });
    load();
  }

  globalThis.__mt_ui = {
    pages: {
      lab: {
        render: function (root, api) {
          renderResult(root, api);
        }
      }
    }
  };
})();

/* Mail Triage composed-UI builder for sandbox bundles (SDK v0.2).
 *
 * A composed plugin's `uiOpen` / `uiDispatch` return a plain JSON component
 * tree built with `MTUIB.*`. The host validates the tree against a strict
 * whitelist and renders it; nothing here executes in a browser, and this file
 * is data-only (no DOM, no network). Unknown props/types are rejected server
 * side, so build only with the helpers below.
 */
(function () {
  "use strict";

  function node(type, props, extra) {
    var out = { type: type };
    if (props) out.props = props;
    if (extra) {
      for (var k in extra) {
        if (extra[k] === undefined || extra[k] === "") continue;
        out[k] = extra[k];
      }
    }
    return out;
  }
  function kids(out, children) {
    if (children && children.length) out.children = children.filter(Boolean);
    return out;
  }

  var B = {
    stack: function (children, opts) {
      opts = opts || {};
      var p = {};
      if (opts.direction) p.direction = opts.direction;
      if (opts.gap) p.gap = opts.gap;
      return kids(node("Stack", p), children);
    },
    grid: function (children, opts) {
      opts = opts || {};
      return kids(node("Grid", opts.cols ? { cols: opts.cols } : null), children);
    },
    split: function (start, end) {
      return kids(node("SplitPane", null), [start, end]);
    },
    tabs: function (tabs, active) {
      var out = node("Tabs", { active: String(active || (tabs[0] && tabs[0].label) || "") });
      out.children = (tabs || []).map(function (t) {
        return kids(node("Tab", { label: String(t.label || "") }), t.children || []);
      });
      return out;
    },
    tab: function (label, children) {
      return kids(node("Tab", { label: String(label || "") }), children);
    },
    text: function (value, opts) {
      opts = opts || {};
      var p = { value: String(value == null ? "" : value) };
      if (opts.variant) p.variant = opts.variant;
      return node("Text", p);
    },
    badge: function (label, opts) {
      opts = opts || {};
      var p = { label: String(label == null ? "" : label) };
      if (opts.variant) p.variant = opts.variant;
      return node("Badge", p);
    },
    button: function (label, opts) {
      opts = opts || {};
      var p = { label: String(label == null ? "" : label) };
      if (opts.variant) p.variant = opts.variant;
      if (opts.disabled) p.disabled = true;
      return node("Button", p, { event: String(opts.event || "") });
    },
    input: function (opts) {
      opts = opts || {};
      var p = {};
      if (opts.name) p.name = String(opts.name);
      if (opts.value != null) p.value = String(opts.value);
      if (opts.placeholder) p.placeholder = String(opts.placeholder);
      if (opts.label) p.label = String(opts.label);
      return node("Input", p, { event: String(opts.event || "") });
    },
    searchField: function (opts) {
      opts = opts || {};
      var p = {};
      if (opts.value != null) p.value = String(opts.value);
      if (opts.placeholder) p.placeholder = String(opts.placeholder);
      if (opts.label) p.label = String(opts.label);
      return node("SearchField", p, { event: String(opts.event || "") });
    },
    menu: function (items) {
      return kids(node("Menu", null), items);
    },
    menuItem: function (label, opts) {
      opts = opts || {};
      var p = { label: String(label == null ? "" : label) };
      if (opts.disabled) p.disabled = true;
      return node("MenuItem", p, { event: String(opts.event || "") });
    },
    dialog: function (title, children, opts) {
      opts = opts || {};
      var p = { title: String(title == null ? "" : title) };
      if (opts.open === false) p.open = false;
      return kids(node("Dialog", p), children);
    },
    list: function (children) {
      return kids(node("List", null), children);
    },
    listItem: function (children, opts) {
      opts = opts || {};
      var p = {};
      if (opts.selected) p.selected = true;
      return kids(node("ListItem", p, { event: String(opts.event || "") }), children);
    },
    separator: function () { return node("Separator", null); },
    state: function (kind, opts) {
      opts = opts || {};
      var p = { kind: String(kind || "empty") };
      if (opts.message != null) p.message = String(opts.message);
      if (opts.actionLabel) p.actionLabel = String(opts.actionLabel);
      return node("StateView", p, { retryEvent: String(opts.retryEvent || "") });
    },
    messageList: function (items, opts) {
      opts = opts || {};
      var p = {};
      if (opts.selected != null) p.selected = opts.selected;
      return node("MessageList", p, { event: String(opts.event || ""), items: items || [] });
    },
    messageReader: function (message) {
      return node("MessageReader", null, { message: message || null });
    }
  };

  globalThis.MTUIB = B;
})();

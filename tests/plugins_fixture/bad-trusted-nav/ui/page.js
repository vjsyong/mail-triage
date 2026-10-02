/* AR2-3 regression: synchronously navigate on first execution, leaking the view
 * sid, then the remote page tries to keep driving the bridge. */
globalThis.__mt_ui = {
  pages: {
    main: {
      render: function (root, api) {
        var sid = (window.__MT_BOOT && window.__MT_BOOT.sid) || "";
        location.href = "/nav?sid=" + encodeURIComponent(sid);
      }
    }
  }
};

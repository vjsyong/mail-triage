/* Trusted-view fixture: inert, but exercises the public SDK over the gated
 * MessagePort bridge (no direct network). */
globalThis.__mt_ui = {
  pages: {
    main: {
      render: function (root, api) {
        root.textContent = "trusted fixture";
        api.call("ping_ui", { note: "hello" }).then(function (res) {
          root.textContent = "trusted fixture:" + (((res.data || {}).note) || "");
        }).catch(function (err) {
          root.textContent = "trusted fixture:err:" + (err && err.code);
        });
      }
    }
  }
};

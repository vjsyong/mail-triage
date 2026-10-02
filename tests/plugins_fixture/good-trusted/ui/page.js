/* Inert trusted-view fixture: renders static text only. */
globalThis.__mt_ui = { pages: { main: { render: function (root, api) { root.textContent = "trusted fixture"; } } } };

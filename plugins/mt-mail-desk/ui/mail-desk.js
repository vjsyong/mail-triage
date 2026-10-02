/* Mail Desk browser page. Uses only the host page SDK (MTUI); it never fetches
 * directly and never touches mail. Rendering is separated from fetching: the
 * components below only build DOM, while the small state machine calls
 * MTUI.call for the two declared read-only operations.
 */
globalThis.__mt_ui = {
  pages: {
    desk: {
      render: function (root, api) {
        var C = api.components;
        var st = {
          q: api.getState().q || "",
          selected: parseInt(api.getState().message, 10) || 0,
          items: [],
          current: null,
          loadingList: false,
          loadingRead: false,
          listError: null,
          readError: null
        };

        var searchHost = document.createElement("div");
        var listHost = document.createElement("div");
        var readerHost = document.createElement("div");
        var split = C.splitPane({ start: listHost, end: readerHost });

        var search = C.searchField({
          value: st.q,
          placeholder: "Search the local index (sender, subject, text)",
          onSearch: function (q) {
            st.q = q;
            st.selected = 0;
            api.updateUrl({ q: q, message: "" }, false);
            runSearch();
          }
        });
        searchHost.appendChild(search);

        var refresh = C.el("button", "mt-btn", "Refresh");
        refresh.type = "button";
        refresh.addEventListener("click", function () {
          if (st.selected) runRead(st.selected);
          runSearch();
        });
        searchHost.appendChild(refresh);

        root.appendChild(searchHost);
        root.appendChild(split);

        function deniedMessage(err) {
          if (err && (err.code === "forbidden" || err.code === "denied")) {
            return "The mailbox read permission was not granted for this plugin.";
          }
          if (err && err.code === "disabled") return "This plugin was disabled.";
          return null;
        }

        function renderList() {
          listHost.textContent = "";
          if (st.loadingList) {
            listHost.appendChild(C.stateView("loading", { message: "Searching\u2026" }));
            return;
          }
          if (st.listError) {
            listHost.appendChild(C.stateView("error", {
              message: st.listError, retry: function () { runSearch(); }
            }));
            return;
          }
          if (!st.items.length) {
            listHost.appendChild(C.stateView("empty", {
              message: st.q ? "No messages match that search."
                            : "Search the local index to begin."
            }));
            return;
          }
          listHost.appendChild(C.messageList({
            items: st.items,
            selected: st.selected,
            onSelect: function (id) { select(id); }
          }));
        }

        function renderReader() {
          readerHost.textContent = "";
          if (st.selected || st.current || st.reading) {
            var back = C.el("button", "mt-btn mt-back", "\u2190 Results");
            back.type = "button";
            back.addEventListener("click", function () { split.showList(); });
            readerHost.appendChild(back);
          }
          if (st.loadingRead) {
            readerHost.appendChild(C.stateView("loading", { message: "Loading\u2026" }));
            return;
          }
          if (st.readError) {
            readerHost.appendChild(C.stateView("error", {
              message: st.readError, retry: function () { runRead(st.selected); }
            }));
            return;
          }
          if (st.current) {
            readerHost.appendChild(C.plainTextReader({ message: st.current }));
            return;
          }
          readerHost.appendChild(C.stateView("empty", {
            message: "Select a message to read its plain-text body."
          }));
        }

        function runSearch() {
          st.loadingList = true;
          st.listError = null;
          renderList();
          api.setStatus("Searching\u2026");
          api.call("search_messages", { query: st.q, limit: 20 }).then(function (res) {
            if (api.isDisposed()) return;
            st.items = (res.data && res.data.messages) || [];
            if (st.selected && !st.items.some(function (m) { return m.id === st.selected; })) {
              st.selected = 0;
            }
            st.loadingList = false;
            api.setStatus(st.items.length + " result(s)");
            renderList();
            if (st.selected) runRead(st.selected);
            else { st.current = null; renderReader(); }
          }).catch(function (err) {
            if (api.isDisposed()) return;
            st.loadingList = false;
            st.listError = deniedMessage(err) || ("Search failed (" + (err.code || "error") + ").");
            api.setStatus("Search failed");
            renderList();
          });
        }

        function runRead(id) {
          st.selected = id;
          st.loadingRead = true;
          st.readError = null;
          split.showReader();
          renderList();
          renderReader();
          api.setStatus("Reading\u2026");
          api.call("read_message", { id: id }).then(function (res) {
            if (api.isDisposed()) return;
            st.current = (res.data && res.data.message) || null;
            st.loadingRead = false;
            api.setStatus("");
            renderReader();
          }).catch(function (err) {
            if (api.isDisposed()) return;
            st.loadingRead = false;
            st.readError = deniedMessage(err) || ("Could not read that message (" + (err.code || "error") + ").");
            api.setStatus("Read failed");
            renderReader();
          });
        }

        function select(id) {
          api.updateUrl({ message: String(id), q: st.q }, false);
          runRead(id);
        }

        api.on("state", function (s) {
          if (api.isDisposed()) return;
          var m = parseInt(s && s.message, 10) || 0;
          st.q = (s && s.q) || "";
          if (search.input) search.input.value = st.q;
          if (m && m !== st.selected) { select(m); }
          else { st.selected = 0; runSearch(); }
        });

        renderList();
        renderReader();
        if (st.selected) {
          runSearch();
        } else if (st.q) {
          runSearch();
        } else {
          api.setStatus("Ready");
        }
      }
    }
  }
};

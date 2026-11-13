/* Folder picker: a read-only server-side directory browser.
 *
 * The app runs on the machine that holds the files, so the browser cannot use a
 * native picker (browsers never expose absolute paths). Any button with
 * `data-path-picker="#selector"` opens this dialog and writes the chosen
 * absolute path into that input.
 */
(function () {
  "use strict";

  const dialog = document.getElementById("folder-picker");
  if (!dialog) return;

  const pathInput = dialog.querySelector("[data-picker-path]");
  const list = dialog.querySelector("[data-picker-list]");
  const crumbs = dialog.querySelector("[data-picker-crumbs]");
  const rootsBar = dialog.querySelector("[data-picker-roots]");
  const statusEl = dialog.querySelector("[data-picker-status]");
  const hiddenBox = dialog.querySelector("[data-picker-hidden]");
  const selectBtn = dialog.querySelector("[data-picker-select]");
  const upBtn = dialog.querySelector("[data-picker-up]");

  const LAST_KEY = "uninterupted.lastFolder";

  let targetInput = null;
  let current = "";
  let parent = null;
  let rows = [];
  let active = -1;
  let rootsLoaded = false;

  function readJson(response) {
    return response.json().then(function (payload) {
      if (!response.ok) {
        throw new Error((payload && payload.detail) || "Request failed");
      }
      return payload;
    });
  }

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "picker-status" + (kind ? " " + kind : "");
  }

  function show(payload) {
    current = payload.path;
    parent = payload.parent;
    pathInput.value = payload.path;
    upBtn.disabled = !parent;
    selectBtn.disabled = false;
    renderCrumbs(payload.path);
    renderRows(payload);
  }

  function renderRows(payload) {
    rows = payload.dirs || [];
    active = -1;
    list.innerHTML = "";
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "picker-empty";
      empty.textContent = "No sub-folders here. Use this folder, or go up.";
      list.appendChild(empty);
    }
    rows.forEach(function (row, index) {
      const li = document.createElement("li");
      li.className = "picker-row";
      li.setAttribute("role", "option");
      li.dataset.index = String(index);
      li.tabIndex = -1;

      const name = document.createElement("span");
      name.className = "picker-name";
      name.textContent = row.name;
      li.appendChild(name);

      if (typeof row.audio === "number" && row.audio > 0) {
        const badge = document.createElement("span");
        badge.className = "picker-badge";
        badge.textContent = row.audio === 1 ? "1 audio file" : row.audio + " audio files";
        li.appendChild(badge);
      }
      li.addEventListener("click", function () {
        open(row.path);
      });
      list.appendChild(li);
    });

    const notes = [];
    if (payload.truncated) notes.push("list truncated; type a path instead");
    if (payload.dirs && payload.dirs.length && payload.dirs[0].audio === null) notes.push("audio counts skipped for large folders");
    setStatus(notes.length ? notes.join(" · ") : "", "");
  }

  function renderCrumbs(path) {
    crumbs.innerHTML = "";
    const parts = path.split("/").filter(Boolean);
    const rootBtn = document.createElement("button");
    rootBtn.type = "button";
    rootBtn.className = "picker-crumb";
    rootBtn.textContent = "/";
    rootBtn.addEventListener("click", function () {
      open("/");
    });
    crumbs.appendChild(rootBtn);

    let walking = "";
    parts.forEach(function (part) {
      walking += "/" + part;
      const target = walking;
      const sep = document.createElement("span");
      sep.className = "picker-crumb-sep";
      sep.textContent = "›";
      crumbs.appendChild(sep);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "picker-crumb";
      btn.textContent = part;
      btn.addEventListener("click", function () {
        open(target);
      });
      crumbs.appendChild(btn);
    });
  }

  function open(path) {
    setStatus("Loading…", "");
    selectBtn.disabled = true;
    const query = new URLSearchParams();
    if (path) query.set("path", path);
    if (hiddenBox.checked) query.set("hidden", "1");
    fetch("/api/fs/list?" + query.toString(), { headers: { Accept: "application/json" } })
      .then(readJson)
      .then(function (payload) {
        show(payload);
        setStatus("", "");
      })
      .catch(function (err) {
        selectBtn.disabled = true;
        setStatus(err.message, "bad");
      });
  }

  function loadRoots() {
    if (rootsLoaded) return Promise.resolve();
    rootsLoaded = true;
    return fetch("/api/fs/roots", { headers: { Accept: "application/json" } })
      .then(readJson)
      .then(function (payload) {
        rootsBar.innerHTML = "";
        (payload.roots || []).forEach(function (root) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "picker-chip";
          btn.textContent = root.name;
          btn.title = root.path;
          btn.addEventListener("click", function () {
            open(root.path);
          });
          rootsBar.appendChild(btn);
        });
      })
      .catch(function () {
        rootsLoaded = false;
      });
  }

  function highlight(next) {
    const nodes = list.querySelectorAll(".picker-row");
    if (!nodes.length) return;
    active = Math.max(0, Math.min(nodes.length - 1, next));
    nodes.forEach(function (node, index) {
      node.classList.toggle("active", index === active);
    });
    nodes[active].scrollIntoView({ block: "nearest" });
  }

  function choose() {
    if (!targetInput || !current) return;
    targetInput.value = current;
    targetInput.dispatchEvent(new Event("input", { bubbles: true }));
    targetInput.dispatchEvent(new Event("change", { bubbles: true }));
    try {
      localStorage.setItem(LAST_KEY, current);
    } catch (err) {
      /* private mode */
    }
    dialog.close();
  }

  function start(pathHint) {
    let start = (pathHint || "").trim();
    if (!start) {
      try {
        start = localStorage.getItem(LAST_KEY) || "";
      } catch (err) {
        start = "";
      }
    }
    loadRoots().then(function () {
      open(start);
    });
  }

  document.addEventListener("click", function (event) {
    const trigger = event.target.closest("[data-path-picker]");
    if (!trigger) return;
    event.preventDefault();
    const selector = trigger.getAttribute("data-path-picker");
    targetInput = selector ? document.querySelector(selector) : null;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    const hint = targetInput ? targetInput.value : "";
    start(hint);
    setTimeout(function () {
      list.focus();
    }, 30);
  });

  dialog.addEventListener("click", function (event) {
    if (event.target.closest("[data-picker-close]")) {
      dialog.close();
      return;
    }
    if (event.target.closest("[data-picker-select]")) {
      choose();
      return;
    }
    if (event.target.closest("[data-picker-up]")) {
      if (parent) open(parent);
      return;
    }
    if (event.target.closest("[data-picker-go]")) {
      open(pathInput.value.trim());
      return;
    }
    if (event.target === dialog) {
      dialog.close();
      return;
    }
    const row = event.target.closest(".picker-row");
    if (row) {
      const index = Number(row.dataset.index);
      if (rows[index]) open(rows[index].path);
    }
  });

  hiddenBox.addEventListener("change", function () {
    open(current);
  });

  pathInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      open(pathInput.value.trim());
    }
  });

  list.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(active + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(active - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (active >= 0 && rows[active]) open(rows[active].path);
    } else if (event.key === "Backspace") {
      event.preventDefault();
      if (parent) open(parent);
    }
  });
})();

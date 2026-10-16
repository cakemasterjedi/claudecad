/* Parts tab: script list, editor, save, check (0 Onshape calls) and 3D preview. See parts_api.py for the routes. */
(function () {
  "use strict";
  const {api, el, registerTab} = window.fsgen;
  const TEMPLATE = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n\n' +
    "export function build(context is Context, id is Id)\n{\n}\n";
  const DRAFT_KEY = "fsgen.parts.draft";

  for (const [tag, attrs] of [["link", {rel: "stylesheet", href: "/static/parts.css"}],
                              ["script", {src: "/static/viewer3d.js", defer: ""}]]) {
    document.head.append(el(tag, attrs));
  }

  registerTab("Parts", mount);

  function mount(panel) {
    const st = {path: null, mtime: null, dirty: false, scripts: [], result: null, scene: 0, viewer: null, busy: false};

    // --- DOM
    const filter = el("input", {type: "search", placeholder: "Filter scripts", "aria-label": "Filter scripts", "class": "p-filter"});
    const listEl = el("ul", {"class": "p-list", role: "list"});
    const newBtn = el("button", {type: "button", "class": "ghost"}, "New");
    const listCard = el("div", {"class": "card p-listcard"},
      el("div", {"class": "p-row"}, el("h2", {}, "Scripts"), newBtn), filter, listEl);

    const title = el("span", {"class": "p-title muted"}, "No script open");
    const dirtyMark = el("span", {"class": "p-dirty", hidden: "", title: "Unsaved changes"}, "unsaved");
    const area = el("textarea", {"class": "p-text", spellcheck: "false", wrap: "off", autocapitalize: "off",
                                 autocomplete: "off", "aria-label": "FeatureScript source", disabled: ""});
    const saveBtn = el("button", {type: "button", "class": "ghost", disabled: ""}, "Save");
    const checkBtn = el("button", {type: "button", disabled: ""}, "Save & check");
    const notice = el("div", {"class": "p-notice", role: "alert", hidden: ""});
    const gutter = el("pre", {"class": "p-gutter", "aria-hidden": "true"}, "1");
    const editCard = el("div", {"class": "card p-editcard"},
      el("div", {"class": "p-row"}, el("div", {"class": "p-file"}, title, dirtyMark), el("div", {"class": "p-actions"}, saveBtn, checkBtn)),
      notice, el("div", {"class": "p-code"}, gutter, area));

    const status = el("div", {"class": "p-status muted", role: "status"}, "Run \"Save & check\" to build locally.");
    const details = el("div", {"class": "p-details"});
    const sceneBar = el("div", {"class": "p-scenes", role: "group", "aria-label": "Scene", hidden: ""});
    const view = el("div", {"class": "p-view", hidden: ""});
    const outCard = el("div", {"class": "card p-outcard"}, el("h2", {}, "Local check"), status, sceneBar, view, details);

    panel.append(el("div", {"class": "parts-root"}, listCard, editCard, outCard));

    // --- helpers
    const setStatus = (text, cls) => { status.className = "p-status " + (cls || "muted"); status.replaceChildren(...text); };
    function showNotice(...kids) { notice.replaceChildren(...kids); notice.hidden = !kids.length; }

    function linkify(text) {  // "script line N" / "line N" -> button that moves the caret to that line
      const out = [], rx = /(?:script )?line (\d+)/gi;
      let last = 0, m;
      while ((m = rx.exec(text))) {
        out.push(text.slice(last, m.index));
        const n = +m[1];
        out.push(el("button", {type: "button", "class": "p-linelink", onclick: () => gotoLine(n)}, m[0]));
        last = m.index + m[0].length;
      }
      out.push(text.slice(last));
      return out;
    }
    function gotoLine(n) {
      const lines = area.value.split("\n");
      n = Math.max(1, Math.min(n, lines.length));
      let pos = 0;
      for (let i = 0; i < n - 1; i++) pos += lines[i].length + 1;
      area.focus();
      area.setSelectionRange(pos, pos + lines[n - 1].length);
      const lh = parseFloat(getComputedStyle(area).lineHeight) || 18;
      area.scrollTop = Math.max(0, (n - 1) * lh - area.clientHeight / 2);
    }

    let gutterLines = 0;
    function syncGutter() {
      const n = area.value.split("\n").length;
      if (n !== gutterLines) { gutterLines = n; gutter.textContent = Array.from({length: n}, (_, i) => i + 1).join("\n"); }
      gutter.scrollTop = area.scrollTop;
    }
    area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });

    function setDirty(d) {
      st.dirty = d; syncGutter();
      dirtyMark.hidden = !d;
      if (d && st.path) {
        try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify({path: st.path, mtime: st.mtime, text: area.value})); } catch (_) {}
      } else { try { sessionStorage.removeItem(DRAFT_KEY); } catch (_) {} }
    }
    const confirmDiscard = () => !st.dirty || window.confirm("Discard unsaved changes to " + st.path + "?");
    window.addEventListener("beforeunload", ev => { if (st.dirty) { ev.preventDefault(); ev.returnValue = ""; } });

    function enable(on) { for (const b of [area, saveBtn, checkBtn]) b.disabled = !on || (b !== area && st.busy); }

    // --- list
    function renderList() {
      const q = filter.value.trim().toLowerCase();
      const split = p => { const i = p.lastIndexOf("/"); return [i < 0 ? "" : p.slice(0, i), p.slice(i + 1)]; };
      const items = st.scripts.filter(p => p.toLowerCase().includes(q)).sort((a, b) => {
        const [da, fa] = split(a), [db, fb] = split(b);
        return da < db ? -1 : da > db ? 1 : fa < fb ? -1 : fa > fb ? 1 : 0;
      });
      const rows = [];
      let dir = null;
      for (const p of items) {  // grouped by folder, file name only
        const i = p.lastIndexOf("/"), d = i < 0 ? "" : p.slice(0, i);
        if (d !== dir) { dir = d; rows.push(el("li", {"class": "p-group"}, d || "(project root)")); }
        rows.push(el("li", {}, el("button", {type: "button", "class": "p-item" + (p === st.path ? " current" : ""), title: p,
          "aria-current": p === st.path ? "true" : "false", onclick: () => open(p)}, p.slice(i + 1))));
      }
      listEl.replaceChildren(...(rows.length ? rows
        : [el("li", {"class": "muted p-none"}, st.scripts.length ? "No match." : "No .fs scripts under the project root.")]));
    }
    async function loadList() {
      try { st.scripts = (await api("/api/scripts")).scripts; renderList(); }
      catch (e) { listEl.replaceChildren(el("li", {"class": "error"}, e.message)); }
    }
    filter.addEventListener("input", renderList);

    // --- open / save
    async function open(path, force) {
      if (!force && !confirmDiscard()) return;
      try {
        const d = await api("/api/scripts/" + path.split("/").map(encodeURIComponent).join("/"));
        st.path = d.path; st.mtime = d.mtime;
        area.value = d.text; setDirty(false); showNotice();
        title.textContent = d.path; title.className = "p-title";
        enable(true); clearResult(); renderList();
      } catch (e) { showNotice(el("span", {}, "Could not open " + path + ": " + e.message)); }
    }
    async function save(overwrite) {
      if (!st.path) return false;
      showNotice();
      try {
        const d = await api("/api/scripts/" + st.path.split("/").map(encodeURIComponent).join("/"),
                            {method: "PUT", body: {text: area.value, mtime: st.mtime}});
        st.mtime = d.mtime; setDirty(false);
        return true;
      } catch (e) {
        if (e.status === 409) { conflict(); return false; }
        showNotice(el("span", {}, "Save failed: " + e.message));
        return false;
      }
    }
    function conflict() {
      showNotice(el("strong", {}, "This file changed on disk since you opened it. "),
        el("button", {type: "button", "class": "ghost", onclick: () => open(st.path, true)}, "Reload (lose my edits)"),
        el("button", {type: "button", onclick: overwrite}, "Overwrite"));
    }
    async function overwrite() {
      try {
        const d = await api("/api/scripts/" + st.path.split("/").map(encodeURIComponent).join("/"));
        st.mtime = d.mtime;
      } catch (e) { showNotice(el("span", {}, "Could not re-read the file: " + e.message)); return; }
      await save();
    }

    area.addEventListener("input", () => setDirty(true));
    area.addEventListener("keydown", ev => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); }
      else if (ev.key === "Tab" && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !ev.shiftKey) {
        ev.preventDefault();
        if (!document.execCommand || !document.execCommand("insertText", false, "    ")) {
          area.setRangeText("    ", area.selectionStart, area.selectionEnd, "end");
          area.dispatchEvent(new Event("input"));
        }
      }
    });
    saveBtn.addEventListener("click", () => save());

    newBtn.addEventListener("click", async () => {
      if (!confirmDiscard()) return;
      const path = (window.prompt("New script path, relative to the project root (must end with .fs):", "new_part.fs") || "").trim();
      if (!path) return;
      if (!path.toLowerCase().endsWith(".fs")) { window.alert("The path must end with .fs"); return; }
      try {
        const d = await api("/api/scripts", {method: "POST", body: {path, text: TEMPLATE}});
        await loadList();
        await open(d.path, true);
      } catch (e) { window.alert(e.status === 409 ? path + " already exists." : "Could not create: " + e.message); }
    });

    // --- check
    function clearResult() {
      st.result = null; details.replaceChildren(); sceneBar.hidden = true; view.hidden = true;
      setStatus(["Run \"Save & check\" to build locally."]);
    }
    function section(label, body, open) {
      return el("details", Object.assign({"class": "p-sec"}, open ? {open: ""} : {}), el("summary", {}, label), body);
    }
    const pre = text => el("pre", {"class": "p-pre"}, text);

    checkBtn.addEventListener("click", async () => {
      if (!st.path || st.busy) return;
      if (st.dirty && !(await save())) return;
      st.busy = true; enable(true);
      setStatus(["Checking... (the first check starts a worker and can take several seconds)"]);
      details.replaceChildren(); sceneBar.hidden = true;
      try {
        const r = await api("/api/check", {method: "POST", body: {path: st.path}});
        st.result = r; showResult(r);
      } catch (e) {
        setStatus([e.status === 504 ? "The check timed out." : "Check failed: " + e.message], "error");
      } finally { st.busy = false; enable(true); }
    });

    function showResult(r) {
      if (r.ok) setStatus([`OK: built ${r.features.length} feature(s) in ${r.seconds} s`], "p-ok");
      else if (r.error) setStatus([`${r.stage === "script" ? "Script error" : "Local build error"}: `, ...linkify(r.error)], "error");
      else setStatus(["Built, but the geometry check found problems (see Validity)."], "error");
      const sec = [];
      if (r.features.length) sec.push(section(`Features (${r.features.length})`,
        el("ul", {"class": "p-feat"}, r.features.map(f => el("li", {}, el("code", {}, f.type), " " + f.name))), !r.ok && r.stage !== "script"));
      if (r.report) sec.push(section("Report", pre(r.report), true));
      if (r.validity_text) sec.push(section("Validity", pre(r.validity_text), r.validity.some(v => !v.ok)));
      if (r.walls_text) sec.push(section(`Thin walls (${r.walls.length})`, pre(r.walls_text), false));
      if (r.print_check) sec.push(section("Print check", pre(r.print_check), false));
      if (r.push_estimate) sec.push(el("p", {"class": "muted p-est"}, r.push_estimate));
      if (r.fit_error) sec.push(el("p", {"class": "error"}, "Fit file: " + r.fit_error));
      details.replaceChildren(...sec);
      sceneBar.replaceChildren(...r.scenes.map((s, i) =>
        el("button", {type: "button", "class": "ghost p-scene", "aria-pressed": String(i === 0), onclick: () => pickScene(i)}, s.name)));
      sceneBar.hidden = r.scenes.length < 2;
      view.hidden = !r.scenes.length;
      if (r.scenes.length) pickScene(0);
    }
    async function pickScene(i) {
      st.scene = i;
      [...sceneBar.children].forEach((b, k) => b.setAttribute("aria-pressed", String(k === i)));
      if (!st.viewer) {
        if (!window.fsgen3d) { await new Promise(r => setTimeout(r, 300)); }
        if (!window.fsgen3d) { view.replaceChildren(el("div", {"class": "v3d-note"}, "3D viewer script did not load.")); return; }
        st.viewer = window.fsgen3d.create(view);
      }
      await st.viewer.show(st.result.scenes[i].parts);
    }

    // --- go
    window.fsgen.openScript = async path => {
      window.fsgen.showTab("Parts");
      await loadList();
      await open(path);
    };
    loadList().then(() => {  // restore an unsaved draft (e.g. after the session expired and the panel was rebuilt)
      let d = null;
      try { d = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null"); } catch (_) {}
      if (d && d.path && typeof d.text === "string") {
        st.path = d.path; st.mtime = d.mtime; area.value = d.text; title.textContent = d.path; title.className = "p-title";
        enable(true); setDirty(true); renderList();
        showNotice(el("span", {}, "Restored your unsaved edits."));
      }
    });
  }
})();

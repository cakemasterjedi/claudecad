/* Workbench tab: history list, 3D view of the SAVED mesh (no rebuild), downloads, push, revise, variables, prompt and log.
   Selection lives in the URL hash: #workbench/<jobId>[/<variant>]. Routes: see generate_api.py. */
(function () {
  "use strict";
  const {api, sse, el, registerTab, css} = window.scriptforge;
  const ui = window.scriptforge.ui;
  css("/static/workbench.css");

  registerTab("Workbench", mount, {onShow: () => window.scriptforge.workbenchShown && window.scriptforge.workbenchShown()});

  const num = n => (n >= 100 ? String(Math.round(n)) : n.toFixed(1).replace(/\.0$/, ""));
  const encode = encodeURIComponent;

  function parseHash() {
    const p = (location.hash || "").replace(/^#/, "").split("/");
    return p[0] === "workbench" && p[1] ? {id: decodeURIComponent(p[1]), variant: p[2] ? decodeURIComponent(p[2]) : null} : {id: null, variant: null};
  }

  function mount(panel) {
    const st = {jobs: [], id: null, variant: null, job: null, seq: 0, stream: null, seen: 0, viewer: null, q: "", hasStl: false};

    // --- DOM
    const search = el("input", {type: "search", placeholder: "wedge, ring, mount…", "aria-label": "Search models", autocomplete: "off"});
    const items = el("div", {"class": "wb-items"});
    const listPane = el("aside", {"class": "wb-list", "aria-label": "History"},
      el("label", {"class": "wb-search"}, el("span", {}, "Search models"), search), items);

    const view = el("div", {"class": "wb-canvas"});
    const chips = el("div", {"class": "wb-chips", role: "group", "aria-label": "Version"});
    const stats = el("div", {"class": "wb-stats", "aria-live": "polite"});
    const msg = el("div", {"class": "wb-msg", role: "status", hidden: ""});
    const stage = el("div", {"class": "wb-stage"}, view, msg, chips, stats);

    const dlStl = el("button", {type: "button"}, "Download STL");
    const dlStep = el("button", {type: "button", "class": "ghost"}, "STEP");
    const dlFs = el("button", {type: "button", "class": "ghost"}, ".fs");
    const dlZip = el("button", {type: "button", "class": "ghost"}, "Zip");
    const pushBtn = el("button", {type: "button", "class": "ghost wb-push"}, "Push to Onshape");
    const actions = el("div", {"class": "wb-actions"}, dlStl, dlStep, dlFs, dlZip, pushBtn);
    const out = el("div", {"class": "wb-out", role: "status", "aria-live": "polite"});

    const change = el("textarea", {rows: "2", maxlength: "2000", "aria-label": "Change it",
      placeholder: "Make the holes 4 mm and add a 1 mm chamfer on the top edge"});
    const reviseBtn = el("button", {type: "button", "class": "wb-revise-btn"}, "Revise");
    const reviseErr = el("p", {"class": "error", role: "alert"});
    const revise = el("div", {"class": "wb-revise"},
      el("label", {"class": "wb-revise-label"}, el("span", {}, "Change it"), change), reviseBtn, reviseErr);
    const pickName = el("span", {"class": "wb-pick-name"}, "Choose a model");
    const pick = el("button", {type: "button", "class": "wb-pick", "aria-expanded": "false"},
      el("span", {"class": "wb-pick-label"}, "Models"), pickName);
    const mainPane = el("section", {"class": "wb-main"}, pick, stage, actions, out, revise);

    const varBody = el("div", {"class": "wb-varbody"});
    const promptBody = el("p", {"class": "wb-prompt"});
    // collapsible sections: all open on a wide screen; on a phone only Variables starts open (less scrolling)
    const isWide = () => window.matchMedia("(min-width: 62rem)").matches, wide = isWide();
    const sec = (title, open, ...kids) => el("details", Object.assign({"class": "wb-sec"}, open || wide ? {open: ""} : {}),
      el("summary", {}, el("h2", {}, title)), ...kids);
    const assumeSec = sec("Assumptions", false, el("div", {"class": "wb-assume"}));
    assumeSec.hidden = true;
    const logBox = el("pre", {"class": "wb-log", "aria-label": "Log"});
    const editLink = el("button", {type: "button", "class": "ghost wb-edit", hidden: ""}, "Open in Scripts editor");
    const logSec = sec("Log", false, logBox), varSec = sec("Variables", true, varBody, editLink);
    const sidePane = el("aside", {"class": "wb-side"},
      varSec,
      sec("Prompt", false, promptBody),
      assumeSec,
      logSec);

    const wb = el("div", {"class": "wb list-open"}, listPane, mainPane, sidePane);
    panel.append(wb);
    function showList(open) {  // phone only (CSS): the list replaces the model view while open
      wb.classList.toggle("list-open", open); pick.setAttribute("aria-expanded", String(open));
      if (open) window.scrollTo(0, 0);
    }
    pick.addEventListener("click", () => showList(!wb.classList.contains("list-open")));
    window.matchMedia("(min-width: 62rem)").addEventListener("change", e => {  // rotated/resized: wide shows every section
      for (const d of sidePane.querySelectorAll("details.wb-sec")) d.open = e.matches || d === varSec;
    });

    // --- helpers
    const note = (...k) => out.replaceChildren(...k);
    const target = () => {  // {label, res} for the shown variant, or the job's own result
      const r = (st.job && st.job.result) || {};
      const vs = r.variants || [];
      if (!vs.length) return {label: null, res: r, variants: []};
      return {label: st.variant, res: vs.find(v => v.label === st.variant) || vs[0], variants: vs};
    };
    function chooseVariant(job, wanted) {
      const vs = (job.result && job.result.variants) || [];
      if (!vs.length) return null;
      if (job.kept) return job.kept.variant;
      if (wanted && vs.some(v => v.label === wanted)) return wanted;
      return (vs.find(v => v.ok && v.script) || vs[0]).label;
    }
    function setHash() {
      if (!st.id) return;
      const h = "#workbench/" + encode(st.id) + (st.variant ? "/" + encode(st.variant) : "");
      if (location.hash !== h) history.replaceState(null, "", h);
    }
    function setMsg(text, kids) { msg.hidden = !text && !kids; msg.replaceChildren(...(text ? [text] : []), ...(kids || [])); }

    // --- list
    function renderList() {
      const q = st.q.trim().toLowerCase();
      const jobs = st.jobs.filter(j => !q || (j.name + " " + j.request).toLowerCase().includes(q));
      const running = jobs.filter(j => j.status === "running");
      const groups = ui.dayGroups(jobs.filter(j => j.status !== "running"));
      if (running.length) groups.unshift({label: "Running", jobs: running});
      const kids = [];
      for (const g of groups) {
        kids.push(el("h3", {"class": "wb-day"}, g.label));
        for (const j of g.jobs) {
          const sel = j.id === st.id;
          kids.push(el("button", {type: "button", "class": "wb-item" + (sel ? " sel" : ""), "aria-current": sel ? "true" : "false",
            onclick: () => choose(j.id, null)},
            ui.thumb(j), el("span", {"class": "wb-item-text"}, el("b", {}, j.name), el("span", {"class": "wb-meta"}, ui.jobTags(j)))));
        }
      }
      if (!kids.length) kids.push(el("p", {"class": "muted wb-none"}, st.jobs.length ? "No match." : "No models yet. Make one in New part."));
      items.replaceChildren(...kids);
    }
    async function loadList() {
      try { st.jobs = await ui.history(); renderList(); }
      catch (e) { items.replaceChildren(el("p", {"class": "error"}, e.message)); }
    }
    search.addEventListener("input", () => { st.q = search.value; renderList(); });

    // --- selection
    function choose(id, variant) {
      if (id === st.id && !variant) { showList(false); return; }
      st.id = id; st.variant = variant; st.job = null;
      setHash(); renderList(); loadDetail();
      if (wb.classList.contains("list-open")) showList(false);
    }
    async function loadDetail() {
      const seq = ++st.seq, id = st.id;
      closeStream();
      logBox.textContent = ""; st.seen = 0;
      resetPane();
      try {
        const job = await api(`/api/generate/${encode(id)}`);
        if (seq !== st.seq) return;
        st.job = job; st.variant = chooseVariant(job, st.variant);
        setHash(); follow(job.id); renderDetail(seq);
      } catch (e) {
        if (seq !== st.seq) return;
        setMsg("Could not open this model: " + e.message);
      }
    }
    function resetPane() {  // everything that depends on the shown model/version (not the log)
      stats.textContent = ""; chips.replaceChildren(); out.replaceChildren(); reviseErr.textContent = "";
      varBody.replaceChildren(); editLink.hidden = true;
      for (const b of [dlStl, dlStep, dlFs, dlZip, pushBtn, reviseBtn]) b.disabled = true;
      pushBtn.textContent = "Push to Onshape";
      st.hasStl = false;
      setMsg("Loading\u2026");
    }
    function renderDetail(seq) {
      const job = st.job, {label, res, variants} = target();
      pickName.textContent = job.name + (label ? " \u00b7 " + label : "");
      promptBody.textContent = job.request || "(no prompt)";
      assumeSec.hidden = !res.assumptions; assumeSec.lastChild.textContent = res.assumptions || "";
      renderChips(job, variants);
      if (job.status === "running") {
        logSec.open = true;
        setMsg("", [ui.spinner(), " Generating\u2026 follow the log" + (isWide() ? " on the right." : " below.")]);
        varBody.replaceChildren(el("p", {"class": "muted"}, "Available when the model is done."));
        return;
      }
      dlZip.disabled = !res.out_dir;
      dlFs.disabled = !res.script;
      reviseBtn.disabled = !(res.ok && res.script);
      if (res.script) {
        editLink.hidden = false;
        editLink.onclick = () => window.scriptforge.openScript && window.scriptforge.openScript(res.script);
      }
      showMesh(seq, job, label, res);
    }
    function renderChips(job, variants) {
      const kids = [];
      for (const v of variants) {
        const kept = job.kept && job.kept.variant === v.label;
        const attrs = {type: "button", "class": "wb-chip" + (v.label === st.variant ? " on" : ""), "aria-pressed": String(v.label === st.variant),
          onclick: () => switchVariant(v.label)};
        if (job.kept && !kept) attrs.disabled = "";
        kids.push(el("button", attrs, v.label + (kept ? " \u00b7 kept" : "")));
      }
      const cur = variants.find(v => v.label === st.variant);
      if (variants.length && !job.kept && job.status === "done" && cur && cur.ok && cur.script) {
        kids.push(el("button", {type: "button", "class": "wb-chip keep", onclick: () => keep(cur.label)}, "Keep " + cur.label));
      }
      chips.replaceChildren(...kids);
    }
    function switchVariant(label) {  // same job, other version: no refetch of the job
      if (label === st.variant) return;
      st.variant = label; setHash();
      const seq = ++st.seq;
      resetPane(); renderDetail(seq);
    }

    async function ensureViewer() {
      if (st.viewer) return st.viewer;
      const lib = await ui.loadViewer();
      if (!lib) return null;
      st.viewer = lib.create(view);
      return st.viewer;
    }
    function showStats(s) {
      stats.replaceChildren(...(s ? [el("span", {}, `${s.size.map(num).join(" × ")} mm`),
        el("span", {}, `${num(s.volume / 1000)} cm³`)] : []));
    }
    async function showMesh(seq, job, label, res) {
      let buf = null;
      if (res.out_dir) {
        try {
          const r = await fetch(ui.downloadUrl(job.id, "stl", label), {credentials: "same-origin", headers: {"X-ScriptForge": "1"}});
          if (r.ok) buf = await r.arrayBuffer();
        } catch (_) { /* offline: treated as no mesh */ }
      }
      if (seq !== st.seq) return;
      if (!buf) {
        st.hasStl = false;
        if (st.viewer) st.viewer.show([]);
        setMsg("No local build for this model", res.out_dir ? [el("span", {"class": "muted"}, " The working files are still available as a zip.")] : []);
        stats.textContent = "";
        loadVariables(seq, res);
        return;
      }
      const viewer = await ensureViewer();
      if (seq !== st.seq) return;
      if (!viewer) { setMsg("The 3D viewer script did not load."); }
      else {
        const s = await viewer.showStl(buf, "#8fb4d9");
        if (seq !== st.seq) return;
        showStats(s);
        setMsg(viewer.failed ? "3D view needs three.js from a CDN and could not load it (offline?)." : "");
      }
      st.hasStl = true;
      dlStl.disabled = dlStep.disabled = false;
      if (res.script) loadEstimate(seq, job, label);
      loadVariables(seq, res);
    }

    // --- variables (lazy, runs the 60 s local check in the background)
    async function loadVariables(seq, res) {
      if (!res.script) { varBody.replaceChildren(el("p", {"class": "muted"}, "No script for this model.")); return; }
      varBody.replaceChildren(el("p", {"class": "muted"}, ui.spinner(), " loading variables…"));
      try {
        const r = await api("/api/check", {method: "POST", body: {path: res.script}});
        if (seq !== st.seq) return;
        const vars = (r && r.variables) || [];
        if (r && r.error && !vars.length) { varBody.replaceChildren(el("p", {"class": "muted"}, "Variables unavailable: " + r.error)); return; }
        const refresh = async c => {  // preview/save: show the rebuilt scene and keep the numbers in sync
          const scenes = (c && c.scenes) || [];
          if (!scenes.length || !st.viewer || seq !== st.seq) return;
          showStats(await st.viewer.show(scenes[0].parts));
          setMsg("");
        };
        const p = window.scriptforge.variablesPanel(res.script, {variables: vars, onPreview: refresh,
          onSaved: c => { refresh(c); note(el("span", {"class": "muted"}, "Saved to the script. The downloads still come from the original build.")); }});
        varBody.replaceChildren(p);
      } catch (e) {
        if (seq === st.seq) varBody.replaceChildren(el("p", {"class": "muted"}, "Variables unavailable: " + (e.status === 504 ? "the check timed out." : e.message)));
      }
    }

    // --- push
    async function loadEstimate(seq, job, label) {
      try {
        const est = await api(`/api/generate/${encode(job.id)}/estimate` + (label ? `?variant=${encode(label)}` : ""));
        if (seq !== st.seq) return;
        st.estimate = est;
        pushBtn.textContent = `Push to Onshape · ~${est.calls} calls`;
      } catch (_) { st.estimate = null; }
      if (seq === st.seq) pushBtn.disabled = false;
    }
    let pushing = false;
    pushBtn.addEventListener("click", async () => {
      const job = st.job, {label, res} = target();
      if (!job || pushing || !res.script) return;
      pushing = true; pushBtn.disabled = true; out.replaceChildren();
      try {
        const est = await api(`/api/generate/${encode(job.id)}/estimate` + (label ? `?variant=${encode(label)}` : ""));
        const name = job.name + (label && !job.kept ? "-" + label : "");
        if (!window.confirm(`Push "${name}" to Onshape? About ${est.calls} API calls (${est.left} of the allocation left).` +
            (est.warnings || []).map(w => `\n\nWarning: ${w}`).join(""))) return;
        note(ui.spinner(), " Pushing to Onshape…");
        const r = await api(`/api/generate/${encode(job.id)}/push`, label ? {method: "POST", body: {variant: label}} : {method: "POST"});
        const kids = [];
        if (r.ok) kids.push(el("span", {"class": "ok"}, "Pushed"), " ", el("a", {href: r.url, target: "_blank", rel: "noopener"}, "Open in Onshape"),
          el("span", {"class": "muted"}, ` (${r.api_calls} API calls)`));
        else kids.push(el("span", {"class": "error"}, r.error || "The push failed."));
        for (const w of r.warnings || []) kids.push(el("p", {"class": "muted"}, "warning: " + w));
        note(...kids);
      } catch (e) { note(el("span", {"class": "error"}, e.message)); }
      finally { pushing = false; pushBtn.disabled = false; }
    });

    // --- downloads (fetched first so a 404 shows a message instead of navigating to JSON)
    async function download(what) {
      const job = st.job, {label} = target();
      if (!job) return;
      out.replaceChildren();
      try {
        const r = await fetch(ui.downloadUrl(job.id, what, label), {credentials: "same-origin", headers: {"X-ScriptForge": "1"}});
        if (!r.ok) { let m = r.statusText; try { m = (await r.json()).error || m; } catch (_) {} note(el("span", {"class": "error"}, m)); return; }
        const m = /filename="?([^";]+)/.exec(r.headers.get("Content-Disposition") || "");
        const url = URL.createObjectURL(await r.blob());
        const a = el("a", {href: url, download: m ? m[1] : `${job.name}.${what}`});
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
      } catch (e) { note(el("span", {"class": "error"}, e.message)); }
    }
    dlStl.addEventListener("click", () => download("stl"));
    dlStep.addEventListener("click", () => download("step"));
    dlFs.addEventListener("click", () => download("script"));
    dlZip.addEventListener("click", () => download("zip"));

    // --- keep / revise
    async function keep(label) {
      if (!window.confirm("Delete the other version's script?")) return;
      try {
        const j = await api(`/api/generate/${encode(st.job.id)}/keep`, {method: "POST", body: {variant: label}});
        st.job = j; st.variant = label;
        loadList(); setHash(); const seq = ++st.seq; resetPane(); renderDetail(seq);
      } catch (e) { note(el("span", {"class": "error"}, e.message)); }
    }
    reviseBtn.addEventListener("click", async () => {
      const text = change.value.trim(), job = st.job, {label} = target();
      if (!text) { reviseErr.textContent = "Describe the change first."; return; }
      reviseErr.textContent = ""; reviseBtn.disabled = true;
      try {
        const body = {change: text};
        if (label) body.variant = label;
        const {id} = await api(`/api/generate/${encode(job.id)}/revise`, {method: "POST", body});
        change.value = "";
        await loadList();
        choose(id, null);
      } catch (e) { reviseErr.textContent = e.message; reviseBtn.disabled = false; }
    });

    // --- log stream (replays from line 0, follows a running job, reloads the job when it finishes)
    function closeStream() { if (st.stream) { st.stream.close(); st.stream = null; } }
    function addLog(text) {
      const stick = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 24;
      logBox.append(text + "\n");
      if (stick) logBox.scrollTop = logBox.scrollHeight;
    }
    function follow(id) {
      closeStream();
      const wasRunning = st.job && st.job.status === "running";
      st.stream = sse(`/api/generate/${encode(id)}/events?from=${st.seen}`, (ev, d) => {
        if (st.id !== id) return;
        if (ev === "log") { if (d.i >= st.seen) { st.seen = d.i + 1; addLog(d.text); } }
        else if (ev === "done") { if (wasRunning) { closeStream(); loadList(); reloadAfterRun(); } }
        else if (ev === "error" && wasRunning) {  // connection lost: resume from the next line
          closeStream();
          setTimeout(() => { if (panel.isConnected && st.id === id) follow(id); }, 2000);
        }
      });
    }
    async function reloadAfterRun() {  // the running job finished: show its model
      const seq = ++st.seq, id = st.id;
      try {
        const job = await api(`/api/generate/${encode(id)}`);
        if (seq !== st.seq) return;
        st.job = job; st.variant = chooseVariant(job, null);
        setHash(); resetPane(); renderDetail(seq);
      } catch (e) { setMsg("Could not open this model: " + e.message); }
    }

    // --- startup / hash / refresh
    async function applyHash() {
      const h = parseHash();
      if (!st.jobs.length) await loadList();
      if (h.id && h.id !== st.id) { st.id = h.id; st.variant = h.variant; st.job = null; renderList(); loadDetail(); showList(false); }
      else if (h.id && h.variant && h.variant !== st.variant && st.job) switchVariant(h.variant);
      else if (!h.id && !st.id && st.jobs.length) {
        const first = st.jobs.find(j => j.status === "running") || st.jobs.find(ui.hasModel) || st.jobs[0];
        choose(first.id, null);
        showList(false);
      }
    }
    window.addEventListener("hashchange", () => { if (!panel.hidden) applyHash(); });
    window.scriptforge.workbenchShown = async () => {
      await loadList();
      if (!parseHash().id && st.id) setHash(); else await applyHash();
    };
    setInterval(() => {  // keep the list fresh while a generation runs elsewhere (New part tab, another device)
      if (!panel.hidden && !document.hidden && st.jobs.some(j => j.status === "running")) loadList();
    }, 5000);
  }
})();

/* scriptforge web shell.

   Contract for tab modules (slices B and C add static/parts.js and static/generate.js):

     window.scriptforge.api(path, {method, body})   -> Promise of parsed JSON. `body` is JSON-encoded; the X-ScriptForge header
                                                 is added to every request. Rejects with Error(message) that has
                                                 .status. A 401 shows the login screen automatically.
     window.scriptforge.sse(path, onEvent, opts)    -> {close()}. Streams text/event-stream from a GET route (or a POST when
                                                 opts = {method:"POST", body:{...}}); onEvent(eventName, dataObject)
                                                 is called per event; the special event "close" fires when the stream
                                                 ends and "error" on a failure.
     window.scriptforge.registerTab(name, mountFn, opts) -> adds a tab. mountFn(panelElement) runs once, when the tab is
                                                 first shown after login. opts: {onShow()} runs every time the tab is
                                                 shown. Tab order: Workbench, Models, Scan, New part, Scripts, Usage (by the
                                                 `order` table below; unknown names go last).
     window.scriptforge.el(tag, attrs, ...children) -> small DOM builder (textContent only; no innerHTML, CSP forbids inline).
     window.scriptforge.showTab(name)               -> switch to a tab (mounting it if needed).
     window.scriptforge.openScript(path)            -> set by parts.js: show the Scripts tab with that script open.
     window.scriptforge.openJob(id, variant)        -> show the Workbench with that generation selected (URL hash).
     window.scriptforge.desktop                     -> true on a desktop browser >= 900px wide (not a phone/tablet); the Scan tab and the
                                                 Workbench ScanRuler items exist only then.
     window.scriptforge.openInScan(url)             -> set by scan.js: show the Scan tab with ScanRuler at a /scanruler/... URL.
     window.scriptforge.uploadScan(file, onProgress) -> set by scan.js: PUT a scan to /api/scans; also listScans(), scanUrl(scan).
     Shared helpers (see the `ui` object below): history(), thumb(), dayGroups(), jobTags(), downloadUrl(),
     startDownload(), loadViewer(), spinner(), when().

   Scripts load with `defer` in this order: app.js, variables.js, usage.js, parts.js, generate.js, scan.js,
   workbench.js, models.js. Never use inline <script> or style="" attributes (CSP); element.style from JS is fine. */
(function () {
  "use strict";
  const order = ["Workbench", "Models", "Scan", "New part", "Scripts", "Usage"];
  const tabs = {};   // name -> {mount, onShow, panel, button, mounted}
  const slug = n => n.toLowerCase().replace(/\s+/g, "-");
  let current = null;

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    for (const k of kids.flat()) e.append(k instanceof Node ? k : String(k));
    return e;
  }

  async function api(path, opts) {
    opts = opts || {};
    const init = {method: opts.method || "GET", headers: {"X-ScriptForge": "1"}, credentials: "same-origin"};
    if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const r = await fetch(path, init);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) {
      if (r.status === 401 && path !== "/api/login") showLogin();
      const err = new Error((data && data.error) || r.statusText);
      err.status = r.status;
      throw err;
    }
    return data;
  }

  function sse(path, onEvent, opts) {
    opts = opts || {};
    const ctl = new AbortController();
    const init = {method: opts.method || "GET", headers: {"X-ScriptForge": "1"}, credentials: "same-origin", signal: ctl.signal};
    if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    (async () => {
      try {
        const r = await fetch(path, init);
        if (!r.ok) {
          if (r.status === 401) showLogin();
          let m = r.statusText; try { m = (await r.json()).error || m; } catch (_) {}
          onEvent("error", {message: m, status: r.status}); return;
        }
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += dec.decode(value, {stream: true});
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i); buf = buf.slice(i + 2);
            let ev = "message", data = [];
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) ev = line.slice(6).trim();
              else if (line.startsWith("data:")) data.push(line.slice(5).trim());
            }
            if (data.length) { try { onEvent(ev, JSON.parse(data.join("\n"))); } catch (e) { onEvent("error", {message: String(e)}); } }
          }
        }
      } catch (e) {
        if (e.name !== "AbortError") onEvent("error", {message: String(e)});
      }
      onEvent("close", {});
    })();
    return {close: () => ctl.abort()};
  }

  function registerTab(name, mount, opts) {
    const panel = el("section", {"class": "panel", hidden: ""});
    const button = el("button", {type: "button", role: "tab", "aria-selected": "false", onclick: () => select(name)}, name);
    tabs[name] = {mount, onShow: (opts && opts.onShow) || null, panel, button, mounted: false};
    const names = Object.keys(tabs).sort((a, b) => rank(a) - rank(b));
    const nav = document.getElementById("tabs"), main = document.getElementById("panels");
    for (const n of names) { nav.append(tabs[n].button); main.append(tabs[n].panel); }
    if (!document.getElementById("app").hidden && !current) select(startTab(names));
  }
  function startTab(names) {  // from the URL hash (#workbench/<id>, #models, ...), else the first tab
    const h = (location.hash || "").replace(/^#/, "").split("/")[0];
    return names.find(n => slug(n) === h) || names[0];
  }
  const rank = n => { const i = order.indexOf(n); return i < 0 ? order.length : i; };

  function select(name) {
    current = name;
    for (const [n, t] of Object.entries(tabs)) {
      const on = n === name;
      t.panel.hidden = !on;
      t.button.setAttribute("aria-selected", on ? "true" : "false");
      if (on && !t.mounted) { t.mounted = true; t.mount(t.panel); }
    }
    if (name !== "Workbench" && location.hash !== "#" + slug(name)) history.replaceState(null, "", "#" + slug(name));
    if (tabs[name] && tabs[name].onShow) tabs[name].onShow();
    refreshStatus();
  }

  // --- header status: Onshape calls left + local model
  function setStatus(id, text) { const e = document.getElementById(id); if (e) e.textContent = text; }
  async function refreshStatus() {
    api("/api/usage").then(u => setStatus("st-calls", `Onshape: ${Number(u.left).toLocaleString()} calls left`),
      () => setStatus("st-calls", "Onshape: unknown")).catch(() => {});
    api("/api/llm").then(d => {
      const ok = !!(d && d.ollama && d.ollama.available);
      document.getElementById("st-dot").className = "dot " + (ok ? "ok" : "bad");
      setStatus("st-model", ok ? "Local model ready" : "Local model unavailable");
    }, () => setStatus("st-model", "Local model unavailable")).catch(() => {});
  }

  function showLogin() {
    document.getElementById("app").hidden = true;
    document.getElementById("login").hidden = false;
    document.getElementById("password").focus();
  }
  function showApp() {
    document.getElementById("login").hidden = true;
    document.getElementById("app").hidden = false;
    const names = Object.keys(tabs).sort((a, b) => rank(a) - rank(b));
    select(current && tabs[current] ? current : startTab(names));
  }

  // --- shared UI helpers for the Workbench, Models and New part tabs
  const svgNS = "http://www.w3.org/2000/svg";
  function placeholder() {  // neutral "no preview" cube
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 26 26"); svg.setAttribute("class", "ph"); svg.setAttribute("aria-hidden", "true");
    for (const d of ["M13 2 23 7.5v11L13 24 3 18.5v-11z", "M3 7.5 13 13l10-5.5M13 13v11"]) {
      const p = document.createElementNS(svgNS, "path");
      p.setAttribute("d", d); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "1.5");
      svg.append(p);
    }
    return svg;
  }
  const spinner = () => el("span", {"class": "spinner", role: "status", "aria-label": "Running"});
  const pad = n => String(n).padStart(2, "0");
  const clock = t => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  function dayLabel(t) {
    const d = new Date(t * 1000), now = new Date();
    const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(now) - day(d)) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    return d.toLocaleDateString([], d.getFullYear() === now.getFullYear() ? {month: "short", day: "numeric"} : {year: "numeric", month: "short", day: "numeric"});
  }
  function dayGroups(jobs) {  // [{label, jobs}] in list order
    const out = [];
    for (const j of jobs) {
      const label = j.started ? dayLabel(j.started) : "Earlier";
      if (!out.length || out[out.length - 1].label !== label) out.push({label, jobs: []});
      out[out.length - 1].jobs.push(j);
    }
    return out;
  }
  const hasModel = j => j.status !== "failed" && (j.ok || (j.scripts || []).length > 0);
  const jobVariant = j => (j.kept && j.kept.variant) || (j.variants && j.variants[0]) || null;
  function jobTags(j) {  // "20:02 · A/B · kept B"
    const t = [j.started ? clock(j.started) : ""];
    if (j.status === "running") t.push("running");
    if (j.variants && j.variants.length) t.push("A/B");
    if (j.kept) t.push("kept " + j.kept.variant);
    if (j.parent) t.push("revision");
    if (j.status === "failed") t.push("failed");
    else if (j.status === "done" && !hasModel(j)) t.push("questions");
    return t.filter(Boolean).join(" \u00b7 ");
  }
  function downloadUrl(id, what, variant) {
    return `/api/generate/${encodeURIComponent(id)}/download?what=${what}` + (variant ? `&variant=${encodeURIComponent(variant)}` : "");
  }
  function startDownload(id, what, variant) {
    const a = el("a", {href: downloadUrl(id, what, variant), download: ""});
    document.body.append(a); a.click(); a.remove();
  }
  function thumb(j) {  // <span class="thumb"> with the newest local review render, or a neutral placeholder
    const box = el("span", {"class": "thumb"});
    if (j.status === "running") { box.append(spinner()); return box; }
    if (!hasModel(j)) { box.append(placeholder()); return box; }
    const img = el("img", {src: downloadUrl(j.id, "thumb", jobVariant(j)), alt: "", loading: "lazy", decoding: "async"});
    img.addEventListener("error", () => box.replaceChildren(placeholder()));
    box.append(img);
    return box;
  }
  const history_ = async () => (await api("/api/generate/history?limit=100")).jobs;
  function loadViewer() {  // resolves to window.scriptforge3d, or null when viewer3d.js did not load
    if (!window.scriptforge3d && !document.querySelector('script[src="/static/viewer3d.js"]')) {
      document.head.append(el("script", {src: "/static/viewer3d.js", defer: ""}));
    }
    return new Promise(resolve => {
      let n = 0;
      const t = setInterval(() => { if (window.scriptforge3d || ++n > 50) { clearInterval(t); resolve(window.scriptforge3d || null); } }, 100);
    });
  }
  function openJob(id, variant) {
    location.hash = "#workbench/" + encodeURIComponent(id) + (variant ? "/" + encodeURIComponent(variant) : "");
    select("Workbench");
  }
  function css(href) { if (!document.querySelector(`link[href="${href}"]`)) document.head.append(el("link", {rel: "stylesheet", href})); }

  // ScanRuler is desktop-only. Phones and tablets (by user agent; iPadOS reports as a touch Mac) and narrow windows get
  // no Scan tab or ScanRuler items. Pointer media queries are not used: headless and some desktop browsers misreport them.
  const ua = navigator.userAgent || "";
  const handheld = (navigator.userAgentData && navigator.userAgentData.mobile) || /Android|iPhone|iPad|iPod|Mobile|Tablet/i.test(ua)
    || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const desktop = !handheld && window.matchMedia("(min-width: 900px)").matches;

  window.scriptforge = {api, sse, registerTab, el, showTab: select, openJob, css, desktop,
    ui: {history: history_, thumb, dayGroups, jobTags, jobVariant, hasModel, downloadUrl, startDownload, loadViewer, spinner, placeholder, clock, slug}};

  window.addEventListener("DOMContentLoaded", () => {
    window.addEventListener("hashchange", () => {  // back/forward or a pasted link between tabs
      const names = Object.keys(tabs), h = (location.hash || "").replace(/^#/, "").split("/")[0];
      const n = names.find(x => slug(x) === h);
      if (n && n !== current && !document.getElementById("app").hidden) select(n);
    });
    document.getElementById("login-form").addEventListener("submit", async ev => {
      ev.preventDefault();
      const err = document.getElementById("login-error"); err.textContent = "";
      try {
        await api("/api/login", {method: "POST", body: {password: document.getElementById("password").value}});
        document.getElementById("password").value = "";
        for (const t of Object.values(tabs)) { t.mounted = false; t.panel.replaceChildren(); }
        showApp();
      } catch (e) { err.textContent = e.message; }
    });
    document.addEventListener("click", ev => {
      const m = document.getElementById("account");
      if (m.open && !m.contains(ev.target)) m.open = false;
    });
    document.getElementById("logout").addEventListener("click", async () => {
      document.getElementById("account").open = false;
      try { await api("/api/logout", {method: "POST"}); } catch (_) {}
      showLogin();
    });
    const pw = document.getElementById("pw"), pwMsg = document.getElementById("pw-msg");
    const pwClose = () => { pw.hidden = true; pw.querySelector("form").reset(); pwMsg.textContent = ""; };
    document.getElementById("change-pw").addEventListener("click", () => {
      document.getElementById("account").open = false;
      pw.hidden = false; document.getElementById("pw-current").focus();
    });
    document.getElementById("pw-cancel").addEventListener("click", pwClose);
    document.getElementById("pw-form").addEventListener("submit", async ev => {
      ev.preventDefault();
      pwMsg.textContent = "";
      const cur = document.getElementById("pw-current").value, nw = document.getElementById("pw-new").value;
      if (nw !== document.getElementById("pw-repeat").value) { pwMsg.textContent = "New passwords differ."; return; }
      try {
        await api("/api/password", {method: "POST", body: {current: cur, new: nw}});
        pwClose();
      } catch (e) { pwMsg.textContent = e.message; }
    });
    api("/api/usage").then(showApp).catch(() => showLogin());
  });
})();

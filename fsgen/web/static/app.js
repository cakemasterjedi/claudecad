/* fsgen web shell.

   Contract for tab modules (slices B and C add static/parts.js and static/generate.js):

     window.fsgen.api(path, {method, body})   -> Promise of parsed JSON. `body` is JSON-encoded; the X-Fsgen header
                                                 is added to every request. Rejects with Error(message) that has
                                                 .status. A 401 shows the login screen automatically.
     window.fsgen.sse(path, onEvent, opts)    -> {close()}. Streams text/event-stream from a GET route (or a POST when
                                                 opts = {method:"POST", body:{...}}); onEvent(eventName, dataObject)
                                                 is called per event; the special event "close" fires when the stream
                                                 ends and "error" on a failure.
     window.fsgen.registerTab(name, mountFn)  -> adds a tab. mountFn(panelElement) runs once, when the tab is first
                                                 shown after login. Tab order: Parts, Generate, Usage (by `order`
                                                 table below; unknown names go last).
     window.fsgen.el(tag, attrs, ...children) -> small DOM builder (textContent only; no innerHTML, CSP forbids inline).
     window.fsgen.showTab(name)               -> switch to a tab (mounting it if needed).
     window.fsgen.openScript(path)            -> set by parts.js: show the Parts tab with that script open.

   Scripts load with `defer` in this order: app.js, usage.js, parts.js, generate.js. parts.js/generate.js may be
   absent: the server answers with an empty script. Never use inline <script> or style="" attributes (CSP). */
(function () {
  "use strict";
  const order = ["Parts", "Generate", "Usage"];
  const tabs = {};   // name -> {mount, panel, button, mounted}
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
    const init = {method: opts.method || "GET", headers: {"X-Fsgen": "1"}, credentials: "same-origin"};
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
    const init = {method: opts.method || "GET", headers: {"X-Fsgen": "1"}, credentials: "same-origin", signal: ctl.signal};
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

  function registerTab(name, mount) {
    const panel = el("section", {"class": "panel", hidden: ""});
    const button = el("button", {type: "button", role: "tab", "aria-selected": "false", onclick: () => select(name)}, name);
    tabs[name] = {mount, panel, button, mounted: false};
    const names = Object.keys(tabs).sort((a, b) => rank(a) - rank(b));
    const nav = document.getElementById("tabs"), main = document.getElementById("panels");
    for (const n of names) { nav.append(tabs[n].button); main.append(tabs[n].panel); }
    if (!document.getElementById("app").hidden && !current) select(names[0]);
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
    select(current && tabs[current] ? current : names[0]);
  }

  window.fsgen = {api, sse, registerTab, el, showTab: select};

  // Placeholders for tabs that later slices fill in (skipped when parts.js / generate.js already registered the tab).
  function placeholder(name) {
    return panel => panel.append(el("div", {"class": "card"}, el("h2", {}, name), el("p", {"class": "muted"}, "Coming soon.")));
  }
  window.addEventListener("DOMContentLoaded", () => {
    for (const n of ["Parts", "Generate"]) if (!tabs[n]) registerTab(n, placeholder(n));
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

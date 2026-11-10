/* Scan tab: hosts ScanRuler (served by ScriptForge at /scanruler/, same origin) in an iframe, plus scan upload/list and updates.
   The iframe is created on first show and kept alive across tab switches (ScanRuler holds unsaved work in memory).
   Exposes on window.scriptforge: openInScan(url, ctx?), uploadScan(file, onProgress), listScans(), scanUrl(scan). */
(function () {
  "use strict";
  const {api, el, registerTab, css, showTab, openJob} = window.scriptforge;
  css("/static/scan.css");

  const ROOT = "/scanruler/";
  const ACCEPT = ".ply,.obj,.stl,.3mf";
  const enc = encodeURIComponent;
  let frame = null, pendingSrc = null, setSrcHook = null;
  let jobCtx = null;  // {jobId, variant, name} of the Workbench model the open scan view belongs to (null: none)
  const MESH_EXT = [".stl", ".3mf", ".ply", ".obj"], TEXT_EXT = [".csv", ".txt"];
  const extOf = n => { const m = /\.[^./\\]+$/.exec(n || ""); return m ? m[0].toLowerCase() : ""; };
  // Fit `head + body + tail` into max chars by cutting the body (marking the cut).
  function fitText(head, body, tail, max) {
    const full = head + body + tail;
    if (full.length <= max) return full;
    const mark = "\u2026(truncated)", room = max - head.length - tail.length - mark.length;
    return head + body.slice(0, Math.max(0, room)) + mark + tail;
  }

  const mb = n => (n >= 1048576 ? (n / 1048576).toFixed(1) : (n / 1048576).toFixed(2)) + " MB";
  const when = t => (t ? new Date(t * 1000).toLocaleDateString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "");
  const scanUrl = s => `/api/scans/${enc(s.id)}/file/${enc(s.name)}`;
  const listScans = async () => ((await api("/api/scans")).scans || []);

  // PUT the raw file; resolves to {id, name, size, uploaded}. onProgress(fraction 0..1).
  function uploadScan(file, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("PUT", "/api/scans?name=" + enc(file.name));
      x.setRequestHeader("X-ScriptForge", "1");
      x.setRequestHeader("Content-Type", "application/octet-stream");
      x.withCredentials = true;
      if (x.upload && onProgress) x.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      x.onerror = () => reject(new Error("Upload failed (network error)."));
      x.onabort = () => reject(new Error("Upload cancelled."));
      x.onload = () => {
        let d = null; try { d = JSON.parse(x.responseText); } catch (_) { /* not JSON */ }
        if (x.status >= 200 && x.status < 300 && d) resolve(d);
        else reject(new Error(x.status === 401 ? "Session expired. Reload and log in again."
          : (d && d.error) || (x.status === 413 ? "That file is too large (500 MB max)." : x.status === 415 ? "Unsupported file type." : "Upload failed (" + x.status + ").")));
      };
      x.send(file);
    });
  }

  function openInScan(url, ctx) {
    jobCtx = ctx && ctx.jobId ? {jobId: ctx.jobId, variant: ctx.variant || null, name: ctx.name || ""} : null;
    pendingSrc = url || ROOT;
    showTab("Scan");
    if (setSrcHook) setSrcHook(pendingSrc);
  }

  window.scriptforge.openInScan = openInScan;
  window.scriptforge.uploadScan = uploadScan;
  window.scriptforge.listScans = listScans;
  window.scriptforge.scanUrl = scanUrl;
  window.scriptforge.scanAccept = ACCEPT;

  registerTab("Scan", mount);

  function mount(panel) {
    const st = {info: null, polling: null, listOpen: false, sendOpen: false, installedShown: null, latest: null};

    const version = el("span", {"class": "scan-version muted"});
    const newWin = el("a", {"class": "scan-link", href: ROOT, target: "_blank", rel: "noopener"}, "Open in new window");
    const updateBtn = el("button", {type: "button", "class": "ghost scan-btn"}, "Update");
    const reloadBtn = el("button", {type: "button", "class": "ghost scan-btn", hidden: ""}, "Reload");
    const note = el("span", {"class": "scan-note", role: "status", "aria-live": "polite"});

    const file = el("input", {type: "file", accept: ACCEPT, "class": "scan-file", "aria-label": "Upload scan file", tabindex: "-1"});
    const uploadBtn = el("button", {type: "button", "class": "ghost scan-btn"}, "Upload scan");
    const progress = el("progress", {"class": "scan-progress", max: "1", value: "0", hidden: "", "aria-label": "Upload progress"});
    const scansBtn = el("button", {type: "button", "class": "ghost scan-btn", "aria-haspopup": "true", "aria-expanded": "false"}, "Scans");
    const scansBody = el("div", {"class": "scan-listbody"});
    const scansMenu = el("div", {"class": "scan-menu", "aria-label": "Uploaded scans", hidden: ""},
      el("p", {"class": "scan-menu-title"}, "Uploaded scans"), scansBody);
    const scansWrap = el("div", {"class": "scan-wrap"}, scansBtn, scansMenu);

    // "Send to ScriptForge" chip: appears after ScanRuler downloads a file we can use (see watchFrame)
    const sendLabel = el("span", {"class": "scan-send-name"});
    const sendBtn = el("button", {type: "button", "class": "ghost scan-btn scan-send-btn", "aria-haspopup": "true", "aria-expanded": "false"}, sendLabel, " \u00b7 Send to ScriptForge \u25BE");
    const sendX = el("button", {type: "button", "class": "ghost scan-btn scan-send-x", "aria-label": "Dismiss"}, "\u00d7");
    const sendMenu = el("div", {"class": "scan-menu", "aria-label": "Send to ScriptForge", hidden: ""});
    const sendWrap = el("div", {"class": "scan-wrap scan-send", hidden: ""}, sendBtn, sendX, sendMenu);

    const toolbar = el("div", {"class": "scan-bar"}, uploadBtn, progress, file, scansWrap, sendWrap, note, el("span", {"class": "scan-spacer"}),
      newWin, reloadBtn, version, updateBtn);
    const body = el("div", {"class": "scan-body"});
    panel.append(el("div", {"class": "scan"}, toolbar, body));

    // --- iframe (lazy, kept alive)
    function ensureFrame() {
      if (frame && frame.isConnected) return;
      frame = el("iframe", {"class": "scan-frame", title: "ScanRuler", src: pendingSrc || ROOT,
        allow: "fullscreen; clipboard-read; clipboard-write", allowfullscreen: ""});
      frame.addEventListener("load", watchFrame);
      body.replaceChildren(frame);
      newWin.href = pendingSrc || ROOT;
    }
    function setSrc(url) {
      newWin.href = url;
      if (st.info && st.info.installed === false) { pendingSrc = url; return; }
      ensureFrame();
      if (frame.getAttribute("src") !== url) frame.setAttribute("src", url);
    }
    setSrcHook = setSrc;
    function showMissing() {
      frame = null;
      body.replaceChildren(el("div", {"class": "scan-empty"},
        el("h2", {}, "ScanRuler is not installed yet"),
        el("p", {"class": "muted"}, "Use Update to download and install it."),
        el("button", {type: "button", onclick: startUpdate}, "Install ScanRuler")));
    }

    // --- info + update
    function render() {
      const i = st.info;
      if (!i) return;
      const newer = !!(i.installed && !i.updating && i.latest && i.version && i.latest !== i.version);
      version.textContent = i.installed && i.version ? "v" + i.version : "";
      updateBtn.disabled = !!i.updating;
      updateBtn.textContent = i.updating ? "Updating…" : (newer ? "Update to " + i.latest : (i.installed ? "Update" : "Install"));
      updateBtn.classList.toggle("ghost", !newer);
      updateBtn.setAttribute("aria-label", newer ? `Update ScanRuler to ${i.latest}` : "Update ScanRuler");
      const msg = [];
      if (i.updating) msg.push("Updating…");
      else if (i.error) msg.push("Update failed: " + i.error);
      note.textContent = msg.join(" ");
      note.classList.toggle("error", !!i.error && !i.updating);
      if (!i.installed) { if (st.installedShown !== false) { st.installedShown = false; showMissing(); } }
      else if (st.installedShown !== true) { st.installedShown = true; ensureFrame(); }
    }
    async function refresh() {
      try { st.info = await api("/api/scanruler"); }
      catch (e) { st.info = null; version.textContent = ""; note.textContent = "Could not read the ScanRuler status: " + e.message; note.classList.add("error"); return null; }
      render();
      return st.info;
    }
    function poll() {
      if (st.polling) return;
      st.polling = setInterval(async () => {
        if (!panel.isConnected) { clearInterval(st.polling); st.polling = null; return; }
        const i = await refresh();
        if (i && !i.updating) {
          clearInterval(st.polling); st.polling = null;
          if (!i.error) {
            note.textContent = i.installed ? `Updated to ${i.version || "the latest version"}. Reload the Scan tab to use it.` : "";
            note.classList.remove("error");
            if (i.installed && frame) reloadBtn.hidden = false;
            if (i.installed && st.installedShown === false) { st.installedShown = true; ensureFrame(); }
          }
        }
      }, 5000);
    }
    async function startUpdate() {
      note.classList.remove("error");
      try { await api("/api/scanruler/update", {method: "POST"}); }
      catch (e) { if (e.status !== 409) { note.textContent = "Update failed: " + e.message; note.classList.add("error"); return; } }
      st.info = Object.assign({}, st.info, {updating: true, error: null});
      render(); poll();
    }
    updateBtn.addEventListener("click", startUpdate);
    reloadBtn.addEventListener("click", () => {
      reloadBtn.hidden = true; note.textContent = "";
      frame = null; ensureFrame();
    });

    // --- uploads
    async function doUpload(f, doneMsg) {
      if (!f) return;
      uploadBtn.disabled = true; progress.hidden = false; progress.value = 0; note.classList.remove("error"); note.textContent = "Uploading " + f.name + "…";
      try {
        const s = await uploadScan(f, p => { progress.value = p; });
        note.textContent = doneMsg || "Uploaded " + s.name + ".";
        if (st.listOpen) loadList();
      } catch (e) { note.textContent = e.message; note.classList.add("error"); }
      finally { uploadBtn.disabled = false; progress.hidden = true; file.value = ""; }
    }
    uploadBtn.addEventListener("click", () => file.click());
    file.addEventListener("change", () => doUpload(file.files[0]));

    // --- ScanRuler -> ScriptForge: watch the (same-origin) iframe for its blob downloads
    // ScanRuler saves every export through <a download href="blob:..."> appended to its body and clicked; the blob URL is revoked
    // after ~10 s, so the fetch must START inside the click handler. The download itself is never prevented.
    function watchFrame() {
      let doc = null;
      try { doc = frame && frame.contentDocument; } catch (_) { /* cross-origin: skip */ }
      if (!doc) return;
      try {
        doc.addEventListener("click", e => {
          const a = e.target && e.target.closest ? e.target.closest("a[download]") : null;
          if (!a || !/^blob:/i.test(a.href)) return;
          const name = a.download || "download", ext = extOf(name);
          if (!MESH_EXT.includes(ext) && !TEXT_EXT.includes(ext)) return;
          // The blob URL belongs to the iframe's document: the parent's fetch() is refused, so read it with the iframe's own.
          doc.defaultView.fetch(a.href).then(r => r.arrayBuffer())
            .then(buf => showSend({name, blob: new Blob([buf])}), () => { /* blob already gone */ });
        }, true);
      } catch (_) { /* inaccessible: skip */ }
    }
    let latest = null;
    function showSend(x) {
      latest = x;
      sendLabel.textContent = x.name;
      sendBtn.setAttribute("aria-label", `Send ${x.name} to ScriptForge`);
      sendWrap.hidden = false;
      openSend(false);
    }
    function hideSend() { latest = null; openSend(false); sendWrap.hidden = true; }
    function openSend(open) {
      st.sendOpen = open; sendMenu.hidden = !open; sendBtn.setAttribute("aria-expanded", String(open));
      if (open) fillSend();
    }
    function fillSend() {
      if (!latest) return;
      const {name, blob} = latest, ext = extOf(name), items = [];
      const item = (label, aria, fn) => el("button", {type: "button", role: "menuitem", "class": "ghost scan-btn scan-send-item", "aria-label": aria,
        onclick: async () => { openSend(false); try { await fn(); } catch (e) { note.textContent = e.message; note.classList.add("error"); } }}, label);
      if (TEXT_EXT.includes(ext)) {
        if (jobCtx) {
          const c = jobCtx;
          items.push(item("Revise " + (c.name || "this model"), "Revise " + (c.name || "this model") + " with these measurements", async () => {
            const text = await blob.text();
            openJob(c.jobId, c.variant);
            window.scriptforge.prefillRevise(fitText(`Measured on the scan in ScanRuler (${name}):\n`, text, "\nAdjust the model to match these measurements.", 2000));
          }));
        }
        items.push(item("New model from these measurements", "Start a new model from these measurements", async () => {
          const text = await blob.text();
          showTab("New part");
          window.scriptforge.prefillGenerate(fitText(`Measurements from a 3D scan (ScanRuler, ${name}):\n`, text, "", 4000));
        }));
      } else if (MESH_EXT.includes(ext)) {
        items.push(item("Save to Scans", "Save " + name + " to Scans", () => doUpload(new File([blob], name), `Saved ${name} to Scans.`)));
      }
      sendMenu.replaceChildren(el("p", {"class": "scan-menu-title"}, "Send to ScriptForge"), ...items);
    }
    sendBtn.addEventListener("click", e => { e.stopPropagation(); openSend(sendMenu.hidden); });
    sendX.addEventListener("click", hideSend);
    document.addEventListener("click", e => { if (st.sendOpen && !sendWrap.contains(e.target)) openSend(false); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && st.sendOpen) { openSend(false); sendBtn.focus(); } });

    // --- scans menu (closes on Escape / outside click; focus returns to the button)
    function openList(open) {
      st.listOpen = open; scansMenu.hidden = !open; scansBtn.setAttribute("aria-expanded", String(open));
      if (open) loadList();
    }
    async function loadList() {
      scansBody.replaceChildren(el("p", {"class": "muted scan-msg"}, "Loading…"));
      try {
        const scans = await listScans();
        if (!scans.length) { scansBody.replaceChildren(el("p", {"class": "muted scan-msg"}, "No scans uploaded yet.")); return; }
        scansBody.replaceChildren(...scans.map(s => el("div", {"class": "scan-row"},
          el("span", {"class": "scan-name"}, el("b", {}, s.name), el("span", {"class": "muted"}, `${mb(s.size)} · ${when(s.uploaded)}`)),
          el("button", {type: "button", "class": "ghost scan-btn", "aria-label": "Open " + s.name + " in ScanRuler",
            onclick: () => { openList(false); openInScan(`${ROOT}?open=${enc(scanUrl(s))}&workspace=elements`); }}, "Open"),
          el("button", {type: "button", "class": "ghost scan-btn scan-del", "aria-label": "Delete " + s.name,
            onclick: async () => {
              if (!window.confirm(`Delete scan "${s.name}"?`)) return;
              try { await api("/api/scans/" + enc(s.id), {method: "DELETE"}); } catch (e) { note.textContent = e.message; note.classList.add("error"); }
              loadList();
            }}, "Delete"))));
      } catch (e) { scansBody.replaceChildren(el("p", {"class": "error scan-msg"}, "Could not load scans: " + e.message)); }
    }
    scansBtn.addEventListener("click", e => { e.stopPropagation(); openList(scansMenu.hidden); });
    document.addEventListener("click", e => { if (st.listOpen && !scansWrap.contains(e.target)) openList(false); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && st.listOpen) { openList(false); scansBtn.focus(); } });

    refresh().then(i => { if (!i) ensureFrame(); if (i && i.updating) poll(); });
  }
})();

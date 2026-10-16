/* Generate tab: describe a part, watch the progress, get a saved script. Runs locally (0 Onshape calls). */
(function () {
  "use strict";
  const {api, sse, el, registerTab} = window.fsgen;
  const link = document.createElement("link");
  link.rel = "stylesheet"; link.href = "/static/generate.css";
  document.head.append(link);

  const fmt = s => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  const MAX_IMAGES = 4, MAX_SIDE = 2000, MAX_TEXT = 4000;

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result).split(",", 2)[1] || "");
      fr.onerror = () => reject(new Error("Could not read the image."));
      fr.readAsDataURL(blob);
    });
  }
  async function loadBitmap(file) {  // honours EXIF orientation
    if (window.createImageBitmap) {
      try { return await createImageBitmap(file, {imageOrientation: "from-image"}); } catch (_) { /* fall back */ }
    }
    const url = URL.createObjectURL(file);
    try {
      return await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img); img.onerror = () => reject(new Error("Not a readable image."));
        img.src = url;
      });
    } finally { URL.revokeObjectURL(url); }
  }
  async function downscale(file) {  // -> JPEG blob, long side <= MAX_SIDE, metadata stripped
    const bmp = await loadBitmap(file);
    const w0 = bmp.naturalWidth || bmp.width, h0 = bmp.naturalHeight || bmp.height;
    const k = Math.min(1, MAX_SIDE / Math.max(w0, h0));
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w0 * k)); c.height = Math.max(1, Math.round(h0 * k));
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);  // flatten transparency
    ctx.drawImage(bmp, 0, 0, c.width, c.height);
    if (bmp.close) bmp.close();
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.88));
    if (!blob) throw new Error("Could not encode the image.");
    return blob;
  }

  registerTab("Generate", panel => {
    const request = el("textarea", {id: "gen-request", maxlength: "4000", rows: "5",
      placeholder: "e.g. A 80 x 50 x 6 mm mounting plate with four 5.5 mm holes 6 mm from the corners and 4 mm rounded corners"});
    const name = el("input", {id: "gen-name", type: "text", maxlength: "60", autocomplete: "off", placeholder: "optional, e.g. mount-plate"});
    const web = el("input", {id: "gen-web", type: "checkbox", checked: ""});
    const fixes = el("input", {id: "gen-fixes", type: "number", min: "0", max: "8", value: "4"});
    const reviews = el("input", {id: "gen-reviews", type: "number", min: "0", max: "3", value: "2"});
    const go = el("button", {type: "submit"}, "Generate");

    // photos & sketches
    const fileInput = el("input", {type: "file", accept: "image/*", multiple: "", "aria-label": "Choose photos"});
    const drop = el("div", {"class": "drop", tabindex: "0", role: "button", "aria-label": "Add photos and sketches"},
      el("span", {"class": "drop-icon", "aria-hidden": "true"}), el("strong", {}, "Photos & sketches"),
      el("span", {}, "Tap to choose or take a photo, drop files here, or paste"),
      el("small", {}, `Optional, up to ${MAX_IMAGES} images`), fileInput);
    const thumbs = el("ul", {"class": "thumbs", hidden: ""});
    const mSel = el("select", {id: "gen-measure-backend", "aria-label": "Measurement reader"},
      el("option", {value: "ollama"}, "Local (Ollama, free)"), el("option", {value: "claude"}, "Claude"));
    const mNote = el("input", {id: "gen-measure-note", type: "text", maxlength: "200", autocomplete: "off",
      "aria-label": "What is this part?", placeholder: "What is this part? (optional)"});
    const mBtn = el("button", {type: "button", "class": "secondary", disabled: ""}, "Read measurements");
    const mSpin = el("span", {"class": "spinner", hidden: ""});
    const mText = el("span", {"class": "muted"}, "");
    const mStatus = el("p", {"class": "mstatus", role: "status", hidden: ""}, mSpin, mText);
    const mErr = el("p", {"class": "error", role: "alert"});
    const measureRow = el("div", {"class": "measure", hidden: ""}, mSel, mNote, mBtn);
    const sendImgs = el("input", {id: "gen-send-images", type: "checkbox", checked: ""});
    const sendRow = el("label", {"class": "check", "for": "gen-send-images", hidden: ""}, sendImgs, "Send photos to the generator too");
    const photos = el("div", {"class": "photos"}, drop, thumbs, measureRow, mStatus, mErr, sendRow);
    const hint = el("p", {"class": "hint", hidden: ""}, "");
    const formErr = el("p", {"class": "error", role: "alert"});
    const form = el("form", {"class": "card"},
      photos,
      el("label", {"for": "gen-request"}, "Describe the part"), hint, request,
      el("label", {"for": "gen-name"}, "Name"), name,
      el("label", {"class": "check", "for": "gen-web"}, web, "Search the web"),
      el("details", {}, el("summary", {}, "Advanced"),
        el("div", {"class": "row"},
          el("div", {}, el("label", {"for": "gen-fixes"}, "Max fixes (0-8)"), fixes),
          el("div", {}, el("label", {"for": "gen-reviews"}, "Review rounds (0-3)"), reviews))),
      el("div", {"class": "actions"}, go, el("span", {"class": "muted note"}, "Takes a few minutes · 0 Onshape calls")), formErr);

    const statusText = el("span", {}, "");
    const elapsed = el("span", {"class": "muted"}, "");
    const logBox = el("pre", {"class": "log", "aria-live": "off"});
    const step = (n, t, d) => el("li", {}, el("span", {"class": "num"}, n), el("div", {}, el("b", {}, t), el("span", {"class": "muted"}, d)));
    const empty = el("div", {"class": "card empty"}, el("span", {"class": "eyebrow"}, "How it works"),
      el("ol", {"class": "steps"},
        step("1", "Describe", "Type the part, or add a photo and let the reader draft the measurements."),
        step("2", "Generate", "Claude writes a FeatureScript; fsgen builds, checks and reviews it locally."),
        step("3", "Open", "The saved script opens in the Parts editor with a 3D preview.")));
    const progress = el("div", {"class": "card progress", hidden: ""},
      el("div", {"class": "status"}, el("span", {"class": "live"}, statusText), elapsed), logBox);
    const resultCard = el("div", {"class": "card", hidden: ""});
    panel.append(el("div", {"class": "page-head"}, el("div", {}, el("h1", {}, "Generate a part"),
      el("p", {}, "Describe it in words, photos or sketches. Everything runs locally."))),
      el("div", {"class": "gen"}, form, el("div", {"class": "gen-side"}, empty, progress, resultCard)));

    let stream = null, timer = null, job = null, seen = 0;
    let running = false, measuring = false, mTimer = null;
    const imgs = [];  // {key, url, id, state: "up"|"ok"|"fail", err}

    function setBusy(busy) {
      running = busy; progress.classList.toggle("running", busy);
      for (const c of [request, name, web, fixes, reviews, go, sendImgs, mSel, mNote]) c.disabled = busy;
      refreshPhotos();
    }
    function refreshPhotos() {
      const lock = running;
      fileInput.disabled = lock || imgs.length >= MAX_IMAGES;
      drop.classList.toggle("disabled", fileInput.disabled);
      drop.setAttribute("aria-disabled", String(fileInput.disabled));
      const okCount = imgs.filter(i => i.state === "ok").length;
      mBtn.disabled = lock || measuring || okCount < 1 || imgs.some(i => i.state === "up");
      mSel.disabled = mNote.disabled = lock || measuring;
      for (const b of thumbs.querySelectorAll("button")) b.disabled = lock;
      thumbs.hidden = !imgs.length; measureRow.hidden = sendRow.hidden = !imgs.length;
    }
    function renderThumbs() {
      thumbs.replaceChildren(...imgs.map(i => el("li", {"class": "thumb " + i.state},
        el("img", {src: i.url, alt: "Uploaded photo"}),
        i.state === "ok" ? "" : el("span", {"class": "tstate", title: i.err || ""}, i.state === "up" ? "Uploading…" : "Failed"),
        el("button", {type: "button", "class": "rm", "aria-label": "Remove photo", onclick: () => removeImg(i)}, "×"))));
      refreshPhotos();
    }
    function removeImg(i) {
      imgs.splice(imgs.indexOf(i), 1); URL.revokeObjectURL(i.url);
      if (!imgs.some(x => x.state === "ok")) sendImgs.checked = true;
      renderThumbs();
    }
    async function addFiles(files) {
      mErr.textContent = "";
      const list = [...files].filter(f => f.type.startsWith("image/") || !f.type);
      if (!list.length) { if (files.length) mErr.textContent = "That is not an image."; return; }
      const room = MAX_IMAGES - imgs.length;
      if (list.length > room) mErr.textContent = `At most ${MAX_IMAGES} images; ignored ${list.length - room}.`;
      for (const f of list.slice(0, Math.max(0, room))) {
        const item = {key: Math.random(), url: "", id: null, state: "up", err: ""};
        imgs.push(item);
        (async () => {
          try {
            const blob = await downscale(f);
            item.url = URL.createObjectURL(blob); renderThumbs();
            const r = await api("/api/uploads", {method: "POST", body: {filename: f.name || "photo.jpg", data: await blobToBase64(blob)}});
            item.id = r.id; item.state = "ok";
          } catch (e) {
            item.state = "fail"; item.err = e.message; mErr.textContent = "Upload failed: " + e.message;
            if (!item.url) item.url = URL.createObjectURL(f);
          }
          renderThumbs();
        })();
      }
      renderThumbs();
    }
    drop.addEventListener("click", ev => { if (ev.target !== fileInput && !fileInput.disabled) fileInput.click(); });
    drop.addEventListener("keydown", ev => { if ((ev.key === "Enter" || ev.key === " ") && !fileInput.disabled) { ev.preventDefault(); fileInput.click(); } });
    fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
    for (const t of ["dragenter", "dragover"]) drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.add("over"); });
    for (const t of ["dragleave", "drop"]) drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.remove("over"); });
    drop.addEventListener("drop", ev => { if (!running) addFiles(ev.dataTransfer.files); });
    panel.addEventListener("paste", ev => {
      if (running || !panel.isConnected) return;
      const files = [...(ev.clipboardData ? ev.clipboardData.files : [])].filter(f => f.type.startsWith("image/"));
      if (files.length) { ev.preventDefault(); addFiles(files); }
    });

    // measurement
    api("/api/llm").then(d => {
      const ok = d && d.ollama && d.ollama.available;
      const o = mSel.querySelector('option[value="ollama"]');
      if (!ok) { o.disabled = true; o.textContent = "Local (Ollama, not running)"; }
      mSel.value = ok && d.measure_default !== "claude" ? "ollama" : "claude";
    }).catch(() => { mSel.value = "claude"; });

    mBtn.addEventListener("click", async () => {
      const ids = imgs.filter(i => i.state === "ok").map(i => i.id);
      if (!ids.length || measuring) return;
      measuring = true; mErr.textContent = ""; hint.hidden = true;
      const t0 = Date.now();
      mSpin.hidden = false; mStatus.hidden = false; mText.textContent = "Reading… 00:00";
      mTimer = setInterval(() => { mText.textContent = "Reading… " + fmt((Date.now() - t0) / 1000); }, 1000);
      refreshPhotos();
      try {
        const body = {images: ids, backend: mSel.value};
        if (mNote.value.trim()) body.note = mNote.value.trim();
        const r = await api("/api/measure", {method: "POST", body});
        insertDraft(String(r.text || "").trim());
        mText.textContent = `Done in ${Math.round(r.seconds || (Date.now() - t0) / 1000)} s (${r.backend || mSel.value}).`;
      } catch (e) { mErr.textContent = e.message; mText.textContent = ""; mStatus.hidden = true; }
      finally { clearInterval(mTimer); mTimer = null; measuring = false; mSpin.hidden = true; refreshPhotos(); }
    });
    function insertDraft(text) {
      if (!text) { mErr.textContent = "The reader returned no text."; return; }
      const cur = request.value.trim();
      let out = cur ? cur + "\n\n" + text : text, cut = false;
      if (out.length > MAX_TEXT) { out = out.slice(0, MAX_TEXT); cut = true; }
      request.value = out;
      request.focus(); request.setSelectionRange(out.length, out.length);
      hint.textContent = "Draft from photo \u2014 check the numbers before generating." + (cut ? " The text was shortened to fit 4000 characters." : "");
      hint.hidden = false;
    }
    function tick() {
      if (job) elapsed.textContent = fmt(((job.finished || Date.now() / 1000) - job.started));
    }
    function addLog(text) {
      const stick = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 24;
      logBox.append(text + "\n");
      if (stick) logBox.scrollTop = logBox.scrollHeight;
    }
    function stopTimers() {
      if (stream) { stream.close(); stream = null; }
      if (timer) { clearInterval(timer); timer = null; }
    }
    function showResult(status, r) {
      r = r || {};
      const kids = [el("h2", {}, "Result"),
        el("p", {"class": r.ok ? "ok" : "bad"}, r.ok ? (r.verified ? "Done: verified locally" : "Done: not fully verified (see the log)") : "Failed")];
      if (r.error) kids.push(el("p", {"class": "error"}, r.error));
      if (r.assumptions) kids.push(el("b", {}, "Assumptions"), el("div", {"class": "assume"}, r.assumptions));
      if (r.script) {
        kids.push(el("p", {}, "Saved as ", el("span", {"class": "path"}, r.script)));
        if (typeof window.fsgen.openScript === "function") {
          kids.push(el("button", {type: "button", onclick: () => window.fsgen.openScript(r.script)}, "Open in editor"));
        }
      }
      if (r.out_dir) kids.push(el("p", {"class": "muted"}, "Working files: ", el("span", {"class": "path"}, r.out_dir)));
      resultCard.replaceChildren(...kids);
      resultCard.hidden = false;
    }
    function finish(status, result) {
      stopTimers();
      if (job) { job.finished = job.finished || Date.now() / 1000; job.status = status; }
      tick();
      statusText.textContent = status === "done" ? "Finished" : "Failed";
      setBusy(false);
      showResult(status, result);
    }
    function connect() {
      stopTimers();
      timer = setInterval(tick, 1000);
      stream = sse(`/api/generate/${encodeURIComponent(job.id)}/events?from=${seen}`, (ev, d) => {
        if (ev === "log") { if (d.i >= seen) { seen = d.i + 1; addLog(d.text); } }
        else if (ev === "done") finish(d.status, d.result);
        else if (ev === "error") {  // connection lost (e.g. the phone slept): resume from the next line
          stopTimers();
          statusText.textContent = "Reconnecting…";
          setTimeout(() => { if (panel.isConnected && job && job.status === "running") connect(); }, 2000);
        }
      });
    }
    function attach(j) {  // replays the log from line 0, then follows it; a finished job just replays
      job = j; seen = 0;
      logBox.textContent = ""; resultCard.hidden = true; progress.hidden = false; empty.hidden = true;
      statusText.textContent = j.status === "running" ? "Running…" : "";
      setBusy(j.status === "running"); tick();
      connect();
    }

    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      formErr.textContent = "";
      const text = request.value.trim();
      if (!text) { formErr.textContent = "Describe the part first."; return; }
      if (imgs.some(i => i.state === "up")) { formErr.textContent = "Wait for the photos to finish uploading."; return; }
      try {
        const body = {request: text, web_search: web.checked,
          max_fixes: Number(fixes.value), review_rounds: Number(reviews.value)};
        if (name.value.trim()) body.name = name.value.trim();
        const ids = imgs.filter(i => i.state === "ok").map(i => i.id);
        if (ids.length && sendImgs.checked) body.images = ids;
        const {id} = await api("/api/generate", {method: "POST", body});
        const {job: j} = await api("/api/generate/current");
        attach(j && j.id === id ? j : {id, started: Date.now() / 1000, status: "running"});
      } catch (e) { formErr.textContent = e.message; }
    });

    (async () => {  // re-attach after reopening the page
      try {
        const {job: j} = await api("/api/generate/current");
        if (!j) return;
        if (j.status === "running") { request.value = j.request; name.value = j.name; }
        attach(j);
      } catch (_) { /* not logged in or no job */ }
    })();
  });
})();

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
    const askBox = el("input", {id: "gen-ask", type: "checkbox"});
    const abBox = el("input", {id: "gen-ab", type: "checkbox"});
    const abA = el("input", {id: "gen-ab-a", type: "text", maxlength: "300", autocomplete: "off",
      "aria-label": "Version A (optional)", placeholder: "e.g. classic signet top"});
    const abB = el("input", {id: "gen-ab-b", type: "text", maxlength: "300", autocomplete: "off",
      "aria-label": "Version B (optional)", placeholder: "e.g. modern flat top"});
    let advanced;
    const abRow = el("div", {"class": "ab-notes", hidden: ""},
      el("div", {}, el("label", {"for": "gen-ab-a"}, "Version A (optional)"), abA),
      el("div", {}, el("label", {"for": "gen-ab-b"}, "Version B (optional)"), abB));

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
    const mQ = el("div", {"class": "mquestions"});
    const measureRow = el("div", {"class": "measure", hidden: ""}, mSel, mNote, mBtn);
    const sendImgs = el("input", {id: "gen-send-images", type: "checkbox", checked: ""});
    const sendRow = el("label", {"class": "check", "for": "gen-send-images", hidden: ""}, sendImgs, "Send photos to the generator too");
    const forceBox = el("input", {id: "gen-force-local", type: "checkbox", disabled: ""});
    const forceSel = el("select", {id: "gen-local-model", "aria-label": "Local model", disabled: ""},
      el("option", {value: ""}, "Ollama not running"));
    const localRow = el("div", {"class": "local"},
      el("label", {"class": "check", "for": "gen-force-local"}, forceBox, "Force local model"), forceSel);
    const photos = el("div", {"class": "photos"}, drop, thumbs, measureRow, mStatus, mQ, mErr, sendRow);
    const hint = el("p", {"class": "hint", hidden: ""}, "");
    const formErr = el("p", {"class": "error", role: "alert"});
    const form = el("form", {"class": "card"},
      photos,
      localRow,
      el("label", {"for": "gen-request"}, "Describe the part"), hint, request,
      el("label", {"for": "gen-name"}, "Name"), name,
      el("label", {"class": "check", "for": "gen-web"}, web, "Search the web"),
      el("label", {"class": "check", "for": "gen-ask"}, askBox, "Let the model ask questions first"),
      advanced = el("details", {}, el("summary", {}, "Advanced"),
        el("label", {"class": "check", "for": "gen-ab"}, abBox, "Make A and B"), abRow,
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
    const recentList = el("ul", {"class": "gen-recent-list"});
    const recent = el("details", {"class": "card gen-recent"}, el("summary", {}, "Recent"), recentList);
    panel.append(el("div", {"class": "page-head"}, el("div", {}, el("h1", {}, "Generate a part"),
      el("p", {}, "Describe it in words, photos or sketches. Everything runs locally."))),
      el("div", {"class": "gen"}, form, el("div", {"class": "gen-side"}, empty, progress, resultCard, recent)));

    let stream = null, timer = null, job = null, seen = 0;
    let running = false, measuring = false, mTimer = null;
    const imgs = [];  // {key, url, id, state: "up"|"ok"|"fail", err}

    let models = [];  // installed Ollama models, from /api/llm
    const store = {
      get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
      set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* storage unavailable */ } },
    };
    askBox.checked = store.get("fsgen.askFirst") !== "0";
    askBox.addEventListener("change", () => store.set("fsgen.askFirst", askBox.checked ? "1" : "0"));
    const syncAB = () => { abRow.hidden = !abBox.checked; };
    abBox.addEventListener("change", syncAB);
    function makeBoth(options) {  // tick A/B and use the first two options as the two versions
      abBox.checked = true; syncAB(); advanced.open = true;
      abA.value = options[0] || ""; abB.value = options[1] || "";
    }
    // Clarifying questions: options as toggle buttons, free text overrides, optional "Make both (A and B)".
    function questionsPanel(questions, buttonText, onSubmit) {
      const states = questions.map(q => ({q, choice: "", both: false}));
      const blocks = states.map(st => {
        const opts = Array.isArray(st.q.options) ? st.q.options : [];
        const text = el("input", {type: "text", maxlength: "500", autocomplete: "off", "aria-label": "Your answer",
          placeholder: opts.length ? "or type your own answer" : "Your answer"});
        const btns = opts.map(o => el("button", {type: "button", "class": "ghost gen-opt", "aria-pressed": "false",
          onclick: () => { st.both = false; st.choice = st.choice === o ? "" : o; text.value = ""; refresh(); }}, o));
        const bothBtn = st.q.compare && opts.length >= 2 ? el("button", {type: "button", "class": "ghost gen-both", onclick: () => {
          st.both = true; st.choice = ""; text.value = ""; makeBoth(opts); refresh(); }}, "Make both (A and B)") : "";
        const done = el("span", {"class": "muted", hidden: ""}, "Both, as versions A and B");
        text.addEventListener("input", () => { if (text.value) { st.both = false; st.choice = ""; } refresh(); });
        function refresh() {
          btns.forEach((b, i) => b.setAttribute("aria-pressed", String(!st.both && st.choice === opts[i])));
          done.hidden = !st.both;
        }
        return el("div", {"class": "gen-question"}, el("p", {}, el("b", {}, st.q.text)),
          el("div", {"class": "gen-opts"}, ...btns, bothBtn), text, done);
      });
      const inputs = blocks.map(b => b.querySelector("input"));
      const btn = el("button", {type: "button"}, buttonText);
      btn.addEventListener("click", () => onSubmit(states.map((st, i) => ({question: st.q.text,
        answer: st.both ? "both, as versions A and B" : (inputs[i].value.trim() || st.choice)}))));
      return el("div", {"class": "gen-questions"}, ...blocks, btn);
    }
    let ollamaOk = false;
    const mOllama = mSel.querySelector('option[value="ollama"]');
    const forced = () => models.length > 0 && forceBox.checked && !!forceSel.value;
    function syncLocal() {  // one place that decides which controls a forced local model locks
      const f = forced(), lock = running || measuring;
      forceBox.disabled = running || measuring || !models.length;
      forceSel.disabled = running || measuring || !models.length || !forceBox.checked;
      mOllama.disabled = !ollamaOk && !f;  // a forced model works even when the configured default is not installed
      if (f) mSel.value = "ollama";
      mSel.disabled = lock || f;
      web.disabled = running || f;
    }
    function setBusy(busy) {
      running = busy; progress.classList.toggle("running", busy);
      for (const c of [request, name, go, fixes, reviews, sendImgs, mNote, askBox, abBox, abA, abB]) c.disabled = busy;
      refreshPhotos();
    }
    function refreshPhotos() {
      const lock = running;
      fileInput.disabled = lock || imgs.length >= MAX_IMAGES;
      drop.classList.toggle("disabled", fileInput.disabled);
      drop.setAttribute("aria-disabled", String(fileInput.disabled));
      const okCount = imgs.filter(i => i.state === "ok").length;
      mBtn.disabled = lock || measuring || okCount < 1 || imgs.some(i => i.state === "up");
      mNote.disabled = lock || measuring;
      syncLocal();
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
    forceBox.addEventListener("change", () => {
      store.set("fsgen.forceLocal", forceBox.checked ? "1" : "0");
      // local models need more repair rounds; only move the untouched defaults
      if (forceBox.checked && fixes.value === "4") fixes.value = "8";
      else if (!forceBox.checked && fixes.value === "8") fixes.value = "4";
      refreshPhotos();
    });
    forceSel.addEventListener("change", () => { store.set("fsgen.localModel", forceSel.value); });
    api("/api/llm").then(d => {
      const ol = (d && d.ollama) || {};
      const ok = !!ol.available;
      models = Array.isArray(ol.models) ? ol.models.filter(m => typeof m === "string") : [];
      ollamaOk = ok;
      if (!ok) mOllama.textContent = models.length ? "Local (Ollama)" : "Local (Ollama, not running)";
      mSel.value = ok && d.measure_default !== "claude" ? "ollama" : "claude";
      if (models.length) {
        forceSel.replaceChildren(...models.map(m => el("option", {value: m}, m)));
        const remembered = store.get("fsgen.localModel");
        forceSel.value = models.includes(remembered) ? remembered : (models.includes(ol.model) ? ol.model : models[0]);
        forceBox.checked = store.get("fsgen.forceLocal") === "1";
        if (forceBox.checked && fixes.value === "4") fixes.value = "8";
      }
      refreshPhotos();
    }).catch(() => { mSel.value = "claude"; });

    mBtn.addEventListener("click", async () => {
      const ids = imgs.filter(i => i.state === "ok").map(i => i.id);
      if (!ids.length || measuring) return;
      measuring = true; mErr.textContent = ""; hint.hidden = true; mQ.replaceChildren();
      const t0 = Date.now();
      mSpin.hidden = false; mStatus.hidden = false; mText.textContent = "Reading… 00:00";
      mTimer = setInterval(() => { mText.textContent = "Reading… " + fmt((Date.now() - t0) / 1000); }, 1000);
      refreshPhotos();
      try {
        const body = {images: ids, backend: mSel.value};
        if (forced()) { body.backend = "ollama"; body.model = forceSel.value; }
        if (mNote.value.trim()) body.note = mNote.value.trim();
        const r = await api("/api/measure", {method: "POST", body});
        insertDraft(String(r.text || "").trim(), !(r.questions && r.questions.length));
        if (r.questions && r.questions.length) {
          mQ.replaceChildren(el("p", {"class": "muted"}, "The reader has questions about the photos:"),
            questionsPanel(r.questions, "Add answers to description", answers => {
              const lines = answers.filter(a => a.answer).map(a => `- Q: ${a.question}\n  A: ${a.answer}`);
              if (lines.length) insertDraft("Answers to your clarifying questions:\n" + lines.join("\n"), false);
              mQ.replaceChildren();
            }));
        }
        mText.textContent = `Done in ${Math.round(r.seconds || (Date.now() - t0) / 1000)} s (${r.model || r.backend || mSel.value}).`;
      } catch (e) { mErr.textContent = e.message; mText.textContent = ""; mStatus.hidden = true; }
      finally { clearInterval(mTimer); mTimer = null; measuring = false; mSpin.hidden = true; refreshPhotos(); }
    });
    function insertDraft(text, complain = true) {
      if (!text) { if (complain) mErr.textContent = "The reader returned no text."; return; }
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
    const viewers = [];
    function loadViewer() {  // same lazy injection as the Parts tab; resolves to window.fsgen3d or null
      if (!window.fsgen3d && !document.querySelector('script[src="/static/viewer3d.js"]')) {
        document.head.append(el("script", {src: "/static/viewer3d.js", defer: ""}));
      }
      return new Promise(resolve => {
        let n = 0;
        const t = setInterval(() => {
          if (window.fsgen3d || ++n > 50) { clearInterval(t); resolve(window.fsgen3d || null); }
        }, 100);
      });
    }
    function previewBlock(script) {
      const view = el("div", {"class": "p-view gen-view", hidden: ""});
      const note = el("p", {"class": "muted gen-prevnote", role: "status"}, "Building the 3D preview…");
      const bar = el("div", {"class": "gen-scenes", role: "group", "aria-label": "Scene", hidden: ""});
      const varSlot = el("div", {"class": "gen-vars", hidden: ""});
      let setScenes = null;  // set once the viewer exists: rebuilds the scene buttons and re-shows the current scene
      let panel = null;
      const refresh = res => {  // after a variables preview/save: new geometry, or keep the old view and show the error
        if (res && res.error) { note.hidden = false; note.textContent = "Build failed: " + res.error; return; }
        note.hidden = true;
        if (setScenes && res && res.scenes && res.scenes.length) setScenes(res.scenes);
      };
      (async () => {
        try {
          const [res, lib] = await Promise.all([
            api("/api/check", {method: "POST", body: {path: script}}), loadViewer()]);
          if (!view.isConnected) return;
          const vars = (res && res.variables) || [];
          if (vars.length && typeof window.fsgen.variablesPanel === "function") {
            panel = window.fsgen.variablesPanel(script, {variables: vars, onPreview: refresh, onSaved: refresh});
            varSlot.replaceChildren(el("b", {}, "Variables"), panel);
            varSlot.hidden = false;
          }
          const scenes = (res && res.scenes) || [];
          if (!scenes.length) { note.textContent = "No 3D preview for this part (it may not build locally)."; return; }
          if (!lib) { note.textContent = "The 3D viewer script did not load."; return; }
          note.hidden = false; note.textContent = "";
          view.hidden = false;
          const viewer = lib.create(view);
          viewers.push(viewer);
          let cur = 0, list = scenes;
          const pick = async i => {
            cur = i;
            [...bar.children].forEach((x, k) => x.setAttribute("aria-pressed", String(k === i)));
            await viewer.show(list[i].parts);
          };
          setScenes = async sc => {
            list = sc; cur = Math.min(cur, sc.length - 1);
            bar.replaceChildren(...sc.map((s2, i) => el("button", {type: "button", "class": "ghost gen-scene",
              "aria-pressed": String(i === cur), onclick: () => pick(i)}, s2.name)));
            bar.hidden = sc.length < 2;
            await pick(cur);
          };
          note.hidden = true;
          await setScenes(scenes);
        } catch (e) { note.textContent = "No 3D preview: " + e.message; }
      })();
      return el("div", {"class": "gen-preview"}, note, bar, view, varSlot);
    }
    function startDownload(jobId, what, variant) {
      const v = variant ? `&variant=${encodeURIComponent(variant)}` : "";
      const a = el("a", {href: `/api/generate/${encodeURIComponent(jobId)}/download?what=${what}${v}`, download: ""});
      document.body.append(a); a.click(); a.remove();
    }
    function pushBlock(jobId, jobName, variant) {
      const out = el("div", {"class": "gen-pushout", role: "status"});
      const pushBtn = el("button", {type: "button"}, "Push to Onshape");
      const bothBtn = el("button", {type: "button", "class": "ghost"}, "Download + push");
      let busy = false;
      async function push(alsoDownload) {
        if (busy) return;
        busy = true; pushBtn.disabled = bothBtn.disabled = true; out.replaceChildren();
        try {
          const est = await api(`/api/generate/${encodeURIComponent(jobId)}/estimate` + (variant ? `?variant=${encodeURIComponent(variant)}` : ""));
          if (!window.confirm(`Push "${jobName}" to Onshape? About ${est.calls} API calls (${est.left} of the allocation left).` + (est.warnings || []).map(w => `\n\nWarning: ${w}`).join(""))) return;
          if (alsoDownload) startDownload(jobId, "zip", variant);
          out.replaceChildren(el("span", {"class": "spinner"}), " Pushing to Onshape…");
          const r = await api(`/api/generate/${encodeURIComponent(jobId)}/push`, variant ? {method: "POST", body: {variant}} : {method: "POST"});
          const kids = [];
          if (r.ok) kids.push(el("span", {"class": "ok"}, "Pushed"), " ",
            el("a", {href: r.url, target: "_blank", rel: "noopener"}, "Open in Onshape"),
            el("span", {"class": "muted"}, ` (${r.api_calls} API calls)`));
          else kids.push(el("span", {"class": "error"}, r.error || "The push failed."));
          for (const w of r.warnings || []) kids.push(el("p", {"class": "muted"}, "warning: " + w));
          out.replaceChildren(...kids);
        } catch (e) { out.replaceChildren(el("span", {"class": "error"}, e.message)); }
        finally { busy = false; pushBtn.disabled = bothBtn.disabled = false; }
      }
      pushBtn.addEventListener("click", () => push(false));
      bothBtn.addEventListener("click", () => push(true));
      return [pushBtn, bothBtn, out];
    }
    function reviseBlock(jobId, label) {
      const ta = el("textarea", {rows: "2", maxlength: "2000", "aria-label": "Change it",
        placeholder: "e.g. make the walls 3 mm and add a lid lip"});
      const btn = el("button", {type: "button", "class": "ghost"}, "Change it");
      const err = el("p", {"class": "error", role: "alert"});
      btn.addEventListener("click", async () => {
        const change = ta.value.trim();
        if (!change) { err.textContent = "Describe the change first."; return; }
        err.textContent = ""; btn.disabled = true;
        try {
          const body = {change}; if (label) body.variant = label;
          const {id} = await api(`/api/generate/${encodeURIComponent(jobId)}/revise`, {method: "POST", body});
          attach(await api(`/api/generate/${encodeURIComponent(id)}`)); loadRecent();
        } catch (e) { err.textContent = e.message; btn.disabled = false; }
      });
      return el("div", {"class": "gen-revise"}, el("label", {}, "Change it", ta), btn, err);
    }
    function resultBlocks(r, jobId, label, jobName) {  // one result (the job's, or one variant's)
      const kids = [];
      if (label) kids.push(el("h3", {}, "Version " + label), el("p", {"class": "muted"}, r.note || ""));
      kids.push(el("p", {"class": r.ok ? "ok" : "bad"}, r.ok ? (r.verified ? "Done: verified locally" : "Done: not fully verified (see the log)") : "Failed"));
      if (r.error) kids.push(el("p", {"class": "error"}, r.error));
      if (r.assumptions) kids.push(el("b", {}, "Assumptions"), el("div", {"class": "assume"}, r.assumptions));
      if (r.script) {
        kids.push(el("p", {}, "Saved as ", el("span", {"class": "path"}, r.script)));
        kids.push(previewBlock(r.script));
      }
      if (jobId && (r.script || r.out_dir)) {
        const acts = [];
        if (r.script) acts.push(el("button", {type: "button", "class": "ghost", onclick: () => startDownload(jobId, "script", label)}, "Download .fs"));
        acts.push(el("button", {type: "button", "class": "ghost", onclick: () => startDownload(jobId, "zip", label)}, "Download all (.zip)"));
        const pushKids = r.script ? pushBlock(jobId, jobName + (label ? "-" + label : ""), label) : [];
        kids.push(el("div", {"class": "gen-actions"}, ...acts, ...pushKids.slice(0, 2)));
        if (pushKids[2]) kids.push(pushKids[2]);
      }
      if (r.ok && r.script && jobId) kids.push(reviseBlock(jobId, label));
      if (r.script && typeof window.fsgen.openScript === "function") {
        kids.push(el("button", {type: "button", onclick: () => window.fsgen.openScript(r.script)}, "Open in editor"));
      }
      if (r.out_dir) kids.push(el("p", {"class": "muted"}, "Working files: ", el("span", {"class": "path"}, r.out_dir)));
      return kids;
    }
    function showResult(status, r) {
      r = r || {};
      while (viewers.length) { try { viewers.pop().dispose(); } catch (_) { /* already gone */ } }
      const kids = [el("h2", {}, "Result")];
      if (job && job.parent) kids.push(parentNote(job.parent));
      const jobId = job && job.id, jobName = (job && job.name) || "part";
      if (r.questions && r.questions.length) {
        if (!request.value.trim() && job && job.request) request.value = job.request;
        kids.push(el("p", {}, "The model has some questions before it designs this."),
          questionsPanel(r.questions, "Continue", answers => submit(answers)));
      } else if (r.variants && r.variants.length && job && job.kept) {
        const w = r.variants.find(v => v.label === job.kept.variant) || r.variants[0];
        kids.push(el("p", {"class": "muted"}, "Kept " + job.kept.variant),
          ...resultBlocks(w, jobId, null, jobName));
      } else if (r.variants && r.variants.length) {
        kids.push(el("div", {"class": "gen-variants"},
          ...r.variants.map(v => el("div", {"class": "gen-variant"}, ...resultBlocks(v, jobId, v.label, jobName),
            v.ok && v.script && jobId ? el("button", {type: "button", onclick: () => keepVariant(v.label)}, "Keep this one") : ""))));
      } else kids.push(...resultBlocks(r, jobId, null, jobName));
      resultCard.replaceChildren(...kids);
      resultCard.hidden = false;
    }
    const when = t => t ? new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
    let recentJobs = [];
    function parentNote(pid) {  // "Revision of <name>", clickable when the parent is in the history
      const p = recentJobs.find(x => x.id === pid);
      return el("p", {"class": "muted"}, "Revision of ", p
        ? el("button", {type: "button", "class": "ghost gen-link", onclick: () => reopen(pid)}, p.name) : "an earlier job");
    }
    async function loadRecent() {
      try {
        const {jobs} = await api("/api/generate/history?limit=30");
        recentJobs = jobs;
        recent.hidden = !jobs.length;
        recentList.replaceChildren(...jobs.map(j => el("li", {},
          el("button", {type: "button", "class": "ghost gen-recent-item", "aria-current": String(!!job && job.id === j.id),
            onclick: () => reopen(j.id)},
            el("b", {}, j.name), " ",
            el("span", {"class": "muted"}, when(j.started) + " · "),
            el("span", {"class": j.status === "running" ? "muted" : (j.ok ? "ok" : "bad")},
              j.status === "running" ? "running" : (j.ok ? "done" : (j.status === "done" ? "questions" : "failed"))),
            j.kept ? el("span", {"class": "muted"}, " · kept " + j.kept.variant) : ""),
          j.parent ? el("span", {"class": "muted gen-rev"}, " revision of ",
            jobs.find(x => x.id === j.parent) ? el("button", {type: "button", "class": "ghost gen-link",
              onclick: () => reopen(j.parent)}, jobs.find(x => x.id === j.parent).name) : "an earlier job") : "")));
      } catch (_) { /* history is optional */ }
    }
    async function reopen(id) {
      if (running && !(job && job.id === id)) { formErr.textContent = "Wait for the running generation to finish."; return; }
      formErr.textContent = "";
      try { attach(await api(`/api/generate/${encodeURIComponent(id)}`)); loadRecent(); }
      catch (e) { formErr.textContent = e.message; }
    }
    async function keepVariant(label) {
      if (!window.confirm("Delete the other version's script?")) return;
      try {
        const j = await api(`/api/generate/${encodeURIComponent(job.id)}/keep`, {method: "POST", body: {variant: label}});
        job.kept = j.kept; job.result = j.result;
        showResult(job.status, j.result);
        loadRecent();
      } catch (e) { window.alert(e.message); }
    }
    function finish(status, result) {
      stopTimers();
      if (job) { job.finished = job.finished || Date.now() / 1000; job.status = status; }
      loadRecent();
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

    async function submit(answers) {  // answers: set when continuing after the model's questions
      formErr.textContent = "";
      const text = (answers && job && job.request) || request.value.trim();
      if (!text) { formErr.textContent = "Describe the part first."; return; }
      if (imgs.some(i => i.state === "up")) { formErr.textContent = "Wait for the photos to finish uploading."; return; }
      try {
        const body = {request: text, web_search: web.checked && !forced(),
          max_fixes: Number(fixes.value), review_rounds: Number(reviews.value)};
        if (name.value.trim()) body.name = name.value.trim();
        if (forced()) body.local_model = forceSel.value;
        const ids = imgs.filter(i => i.state === "ok").map(i => i.id);
        if (ids.length && sendImgs.checked) body.images = ids;
        if (abBox.checked) body.variants = [abA.value.trim(), abB.value.trim()];
        if (answers) { body.answers = answers; body.ask = false; } else body.ask = askBox.checked;
        const {id} = await api("/api/generate", {method: "POST", body});
        const {job: j} = await api("/api/generate/current");
        attach(j && j.id === id ? j : {id, request: text, started: Date.now() / 1000, status: "running"});
      } catch (e) { formErr.textContent = e.message; }
    }
    form.addEventListener("submit", ev => { ev.preventDefault(); submit(null); });

    loadRecent();
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

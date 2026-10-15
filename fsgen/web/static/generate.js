/* Generate tab: describe a part, watch the progress, get a saved script. Runs locally (0 Onshape calls). */
(function () {
  "use strict";
  const {api, sse, el, registerTab} = window.fsgen;
  const link = document.createElement("link");
  link.rel = "stylesheet"; link.href = "/static/generate.css";
  document.head.append(link);

  const fmt = s => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  registerTab("Generate", panel => {
    const request = el("textarea", {id: "gen-request", maxlength: "4000", rows: "5",
      placeholder: "e.g. A 80 x 50 x 6 mm mounting plate with four 5.5 mm holes 6 mm from the corners and 4 mm rounded corners"});
    const name = el("input", {id: "gen-name", type: "text", maxlength: "60", autocomplete: "off", placeholder: "optional, e.g. mount-plate"});
    const web = el("input", {id: "gen-web", type: "checkbox", checked: ""});
    const fixes = el("input", {id: "gen-fixes", type: "number", min: "0", max: "8", value: "4"});
    const reviews = el("input", {id: "gen-reviews", type: "number", min: "0", max: "3", value: "2"});
    const go = el("button", {type: "submit"}, "Generate");
    const formErr = el("p", {"class": "error", role: "alert"});
    const form = el("form", {"class": "card"},
      el("h2", {}, "Generate a part"),
      el("label", {"for": "gen-request"}, "Describe the part"), request,
      el("label", {"for": "gen-name"}, "Name"), name,
      el("label", {"class": "check", "for": "gen-web"}, web, "Search the web"),
      el("details", {}, el("summary", {}, "Advanced"),
        el("div", {"class": "row"},
          el("div", {}, el("label", {"for": "gen-fixes"}, "Max fixes (0-8)"), fixes),
          el("div", {}, el("label", {"for": "gen-reviews"}, "Review rounds (0-3)"), reviews))),
      go, formErr,
      el("p", {"class": "muted note"}, "Runs locally: Claude writes the script, fsgen checks and reviews it. 0 Onshape calls. Takes a few minutes."));

    const statusText = el("span", {}, "");
    const elapsed = el("span", {"class": "muted"}, "");
    const logBox = el("pre", {"class": "log", "aria-live": "off"});
    const progress = el("div", {"class": "card", hidden: ""}, el("h2", {}, "Progress"),
      el("div", {"class": "status"}, statusText, elapsed), logBox);
    const resultCard = el("div", {"class": "card", hidden: ""});
    panel.append(el("div", {"class": "gen"}, form, progress, resultCard));

    let stream = null, timer = null, job = null, seen = 0;

    function setBusy(busy) {
      for (const c of [request, name, web, fixes, reviews, go]) c.disabled = busy;
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
      logBox.textContent = ""; resultCard.hidden = true; progress.hidden = false;
      statusText.textContent = j.status === "running" ? "Running…" : "";
      setBusy(j.status === "running"); tick();
      connect();
    }

    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      formErr.textContent = "";
      const text = request.value.trim();
      if (!text) { formErr.textContent = "Describe the part first."; return; }
      try {
        const body = {request: text, web_search: web.checked,
          max_fixes: Number(fixes.value), review_rounds: Number(reviews.value)};
        if (name.value.trim()) body.name = name.value.trim();
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

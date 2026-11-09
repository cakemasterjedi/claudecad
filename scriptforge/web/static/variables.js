/* Variables panel: edit a script's variable(...) values, rebuild locally (0 Onshape calls) and save them into the script.
   Plain script (no modules): loaded after app.js, exposes window.scriptforge.variablesPanel. Routes: see parts_api.py.
   Editing a value previews it automatically after a short pause; Save and Reset show only while something is changed. */
(function () {
  "use strict";
  const {api, el} = window.scriptforge;
  const PAUSE = 700;  // ms after the last keystroke before the live preview rebuilds

  // wall_thickness / wallThickness -> "Wall thickness"
  function humanize(name) {
    const s = String(name).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[_\-.]+/g, " ").trim().toLowerCase();
    return s ? s[0].toUpperCase() + s.slice(1) : String(name);
  }
  // "4 * mm" / "30 deg" -> {value: "4", unit: "mm"}; anything else stays a plain expression
  const UNITS = {mm: "mm", millimeter: "mm", cm: "cm", centimeter: "cm", m: "m", meter: "m", inch: "in", in: "in", deg: "°", degree: "°", rad: "rad", radian: "rad"};
  function splitUnit(expr) {
    const m = /^\s*(-?\d+(?:\.\d+)?)\s*\*?\s*([a-z]+)\s*$/i.exec(expr || "");
    if (m && UNITS[m[2].toLowerCase()]) return {value: m[1], unit: m[2], label: UNITS[m[2].toLowerCase()]};
    return null;
  }

  /* variablesPanel(scriptPath, {variables, onPreview(check), onSaved(check)}) -> HTMLElement
     variables: the `variables` array of a /api/check result for this script (name, expression, type, description).
     The element also has .update(variables) to re-render after the caller re-checks the script. */
  function variablesPanel(scriptPath, opts) {
    opts = opts || {};
    let vars = [], rows = {}, timer = null, runSeq = 0, previewed = "";
    const status = el("p", {"class": "muted var-status", role: "status"});
    const err = el("p", {"class": "error", role: "alert", hidden: ""});
    const list = el("div", {"class": "var-list"});
    const resetBtn = el("button", {type: "button", "class": "ghost"}, "Reset");
    const saveBtn = el("button", {type: "button"}, "Save values");
    const bar = el("div", {"class": "row var-bar", hidden: ""}, resetBtn, saveBtn);
    const root = el("div", {"class": "var-panel"}, list, status, err, bar);

    const exprOf = name => {  // what the user typed, with the unit chip put back
      const r = rows[name], val = r.input.value.trim();
      return r.unit && /^-?\d+(\.\d+)?$/.test(val) ? `${val} * ${r.unit}` : val;
    };
    const changed = () => {
      const out = {};
      for (const v of vars) {
        const r = rows[v.name], val = r.input.value.trim();
        if (val !== r.original) out[v.name] = exprOf(v.name);
      }
      return out;
    };
    const showError = msg => { err.textContent = msg || ""; err.hidden = !msg; };
    function refreshDirty() {
      const c = changed(), n = Object.keys(c).length;
      for (const v of vars) rows[v.name].row.classList.toggle("dirty", v.name in c);
      bar.hidden = !n;
      return c;
    }

    async function run(method, url, values, doneText) {
      const seq = ++runSeq;
      showError("");
      if (method === "PUT") saveBtn.disabled = resetBtn.disabled = true;
      status.textContent = method === "PUT" ? "Building and saving…" : "Updating preview…";
      try {
        const res = await api(url, {method, body: {path: scriptPath, variables: values}});
        if (seq !== runSeq) return;  // a newer edit superseded this build
        if (res.error) { showError(res.error); status.textContent = ""; return; }
        status.textContent = doneText;
        if (method === "PUT") { root.update(res.variables || []); if (opts.onSaved) opts.onSaved(res); }
        else if (opts.onPreview) opts.onPreview(res);
      } catch (e) { if (seq === runSeq) { showError(e.message); status.textContent = ""; } }
      finally { if (method === "PUT") saveBtn.disabled = resetBtn.disabled = false; }
    }
    function schedulePreview() {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const values = changed(), key = JSON.stringify(values);
        if (key === previewed) return;
        previewed = key;
        if (!Object.keys(values).length) { status.textContent = ""; showError(""); run("POST", "/api/check", {}, ""); return; }
        run("POST", "/api/check", values, "Preview (not saved yet).");
      }, PAUSE);
    }
    saveBtn.addEventListener("click", () => {
      clearTimeout(timer);
      const values = changed();
      if (!Object.keys(values).length) return;
      run("PUT", "/api/variables", values, "Saved.");
    });
    resetBtn.addEventListener("click", () => {
      for (const v of vars) rows[v.name].input.value = rows[v.name].original;
      refreshDirty(); schedulePreview();
    });

    root.update = function (variables) {
      vars = (variables || []).slice(); rows = {}; previewed = "";
      list.replaceChildren(...vars.map(v => {
        const u = splitUnit(v.expression);
        const original = u ? u.value : v.expression;
        const input = el("input", {type: "text", value: original, maxlength: "60", spellcheck: "false", autocomplete: "off",
          inputmode: u ? "decimal" : "text", "aria-label": humanize(v.name) + (u ? ` (${u.label})` : "")});
        const field = el("span", {"class": "var-field"}, input, u ? el("span", {"class": "var-unit"}, u.label) : "");
        const row = el("label", {"class": "var-row"},
          el("span", {"class": "var-text"},
            el("span", {"class": "var-name"}, humanize(v.name), el("span", {"class": "var-dot", "aria-hidden": "true"})),
            el("code", {"class": "var-code"}, v.name)),
          field,
          v.description ? el("span", {"class": "muted var-desc"}, v.description) : "");
        input.addEventListener("input", () => { refreshDirty(); schedulePreview(); });
        input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); previewed = ""; schedulePreview(); } });
        rows[v.name] = {row, input, original, unit: u ? u.unit : null};
        return row;
      }));
      if (!vars.length) list.append(el("p", {"class": "muted"}, "This script has no variables."));
      refreshDirty();
    };
    root.update(opts.variables);
    return root;
  }

  window.scriptforge.variablesPanel = variablesPanel;
  window.scriptforge.humanizeVar = humanize;
})();

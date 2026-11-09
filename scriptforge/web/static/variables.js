/* Variables panel: edit a script's variable(...) values, rebuild locally (0 Onshape calls) and save them into the script.
   Plain script (no modules): loaded after app.js, exposes window.scriptforge.variablesPanel. Routes: see parts_api.py. */
(function () {
  "use strict";
  const {api, el} = window.scriptforge;

  /* variablesPanel(scriptPath, {variables, onPreview(check), onSaved(check)}) -> HTMLElement
     variables: the `variables` array of a /api/check result for this script (name, expression, type, description).
     The element also has .update(variables) to re-render after the caller re-checks the script. */
  function variablesPanel(scriptPath, opts) {
    opts = opts || {};
    let vars = [], inputs = {};
    const status = el("p", {"class": "muted", role: "status"});
    const err = el("p", {"class": "error", role: "alert", hidden: ""});
    const list = el("div", {"class": "var-list"});
    const previewBtn = el("button", {type: "button", "class": "ghost"}, "Preview");
    const saveBtn = el("button", {type: "button"}, "Save values");
    const root = el("div", {"class": "var-panel"}, list, el("div", {"class": "row"}, previewBtn, saveBtn), status, err);

    const changed = () => {
      const out = {};
      for (const v of vars) {
        const val = inputs[v.name].value.trim();
        if (val !== v.expression) out[v.name] = val;
      }
      return out;
    };
    const showError = msg => { err.textContent = msg || ""; err.hidden = !msg; };
    const busy = b => { previewBtn.disabled = saveBtn.disabled = b; };

    async function run(method, url, done, doneText) {
      showError("");
      const values = changed();
      if (!Object.keys(values).length) { status.textContent = "No values changed."; return; }
      busy(true); status.textContent = method === "PUT" ? "Building and saving…" : "Building…";
      try {
        const res = await api(url, {method, body: {path: scriptPath, variables: values}});
        if (res.error) { showError(res.error); status.textContent = ""; return; }
        status.textContent = doneText;
        if (method === "PUT") root.update(res.variables || []);
        if (done) done(res);
      } catch (e) { showError(e.message); status.textContent = ""; }
      finally { busy(false); }
    }
    previewBtn.addEventListener("click", () => run("POST", "/api/check", opts.onPreview, "Preview updated (not saved)."));
    saveBtn.addEventListener("click", () => run("PUT", "/api/variables", opts.onSaved, "Saved."));

    root.update = function (variables) {
      vars = (variables || []).slice(); inputs = {};
      list.replaceChildren(...vars.map(v => {
        const input = el("input", {type: "text", value: v.expression, maxlength: "60", spellcheck: "false",
                                   "aria-label": v.name, autocomplete: "off"});
        inputs[v.name] = input;
        return el("label", {"class": "var-row"}, el("span", {"class": "var-name"}, v.name),
          el("span", {"class": "muted var-type"}, v.type.toLowerCase()), input,
          v.description ? el("span", {"class": "muted var-desc"}, v.description) : "");
      }));
      if (!vars.length) list.append(el("p", {"class": "muted"}, "This script has no variables."));
      previewBtn.disabled = saveBtn.disabled = !vars.length;
    };
    root.update(opts.variables);
    return root;
  }

  window.scriptforge.variablesPanel = variablesPanel;
})();

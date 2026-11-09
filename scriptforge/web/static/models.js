/* Models tab: grid of every generation (newest first, grouped by day). Open -> Workbench; STL -> download of the saved mesh. */
(function () {
  "use strict";
  const {api, el, registerTab, css, openJob} = window.scriptforge;
  const ui = window.scriptforge.ui;
  css("/static/models.css");

  const FILTERS = [
    ["all", "All", () => true],
    ["ab", "A/B", j => !!(j.variants && j.variants.length)],
    ["rev", "Revisions", j => !!j.parent],
    ["failed", "Failed", j => j.status === "failed"],
  ];

  registerTab("Models", panel => {
    const st = {jobs: [], filter: "all", q: ""};
    const count = el("span", {"class": "muted"});
    const search = el("input", {type: "search", placeholder: "name or prompt", "aria-label": "Search models", autocomplete: "off"});
    const chipBtns = FILTERS.map(([key, label]) => el("button", {type: "button", "class": "m-chip", "aria-pressed": String(key === st.filter),
      onclick: () => { st.filter = key; render(); }}, label));
    const grid = el("div", {"class": "m-groups"});
    panel.append(el("div", {"class": "models"},
      el("div", {"class": "m-head"}, el("h1", {}, "Models"), count, el("label", {"class": "m-search"}, el("span", {}, "Search"), search)),
      el("div", {"class": "m-chips", role: "group", "aria-label": "Filter"}, ...chipBtns), grid));
    search.addEventListener("input", () => { st.q = search.value; render(); });

    function card(j) {
      const v = ui.jobVariant(j), can = ui.hasModel(j);
      const stl = el("button", {type: "button", "class": "ghost", onclick: () => ui.startDownload(j.id, "stl", v)}, "STL");
      if (!can) { stl.disabled = true; stl.title = "No local build for this model"; }
      return el("article", {"class": "m-card"},
        el("button", {type: "button", "class": "m-thumb", "aria-label": "Open " + j.name, onclick: () => openJob(j.id, v)}, ui.thumb(j)),
        el("div", {"class": "m-body"},
          el("div", {"class": "m-name"}, el("b", {title: j.name}, j.name)),
          el("span", {"class": "m-meta"}, ui.jobTags(j)),
          el("div", {"class": "m-btns"}, el("button", {type: "button", onclick: () => openJob(j.id, v)}, "Open"), stl)));
    }
    function render() {
      chipBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(FILTERS[i][0] === st.filter)));
      const f = FILTERS.find(x => x[0] === st.filter)[2], q = st.q.trim().toLowerCase();
      const jobs = st.jobs.filter(j => f(j) && (!q || (j.name + " " + j.request).toLowerCase().includes(q)));
      count.textContent = `${jobs.length} of ${st.jobs.length} models · opens the saved mesh, nothing is rebuilt`;
      const kids = ui.dayGroups(jobs).map(g => el("section", {"class": "m-group"},
        el("h2", {}, g.label), el("div", {"class": "m-grid"}, g.jobs.map(card))));
      grid.replaceChildren(...(kids.length ? kids : [el("p", {"class": "muted"}, st.jobs.length ? "No match." : "No models yet. Make one in New part.")]));
    }
    async function load() {
      try { st.jobs = await ui.history(); render(); }
      catch (e) { grid.replaceChildren(el("p", {"class": "error"}, e.message)); }
    }
    window.scriptforge.modelsShown = load;
    load();
  }, {onShow: () => window.scriptforge.modelsShown && window.scriptforge.modelsShown()});
})();

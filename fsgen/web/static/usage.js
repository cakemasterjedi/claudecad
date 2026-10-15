/* Usage tab: Onshape API allocation (read-only, from GET /api/usage). */
(function () {
  "use strict";
  const {api, el, registerTab} = window.fsgen;

  function table(title, rows) {
    if (!rows.length) return el("p", {"class": "muted"}, `${title}: none logged`);
    return el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, title), el("th", {"class": "n"}, "Calls"))),
      el("tbody", {}, rows.map(r => el("tr", {}, el("td", {}, r.name), el("td", {"class": "n"}, r.count)))));
  }

  registerTab("Usage", panel => {
    const body = el("div", {});
    const refresh = el("button", {type: "button", "class": "ghost", onclick: load}, "Refresh");
    panel.append(el("div", {"class": "card"}, el("h2", {}, "Onshape API usage"), body, refresh));
    async function load() {
      try {
        const u = await api("/api/usage");
        const fill = el("div", {"class": u.percent >= 90 ? "high" : ""});
        fill.style.width = Math.min(100, u.percent) + "%";
        body.replaceChildren(
          el("div", {"class": "meter", role: "img", "aria-label": `${u.percent}% used`}, fill),
          el("div", {"class": "stats"},
            el("span", {}, el("b", {}, u.used), ` of ${u.allocation} used (${u.percent}%)`),
            el("span", {}, el("b", {}, u.left), " left"),
            el("span", {"class": "muted"}, `${u.logged} logged since baseline`)),
          table("By command", u.by_purpose), el("p", {}), table("Top endpoints", u.top_endpoints));
      } catch (e) { body.replaceChildren(el("p", {"class": "error"}, e.message)); }
    }
    load();
  });
})();

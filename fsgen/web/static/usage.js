/* Usage tab: Onshape API allocation (read-only, from GET /api/usage). */
(function () {
  "use strict";
  const {api, el, registerTab} = window.fsgen;

  function endpoint(name) {  // "POST partstudios/..." -> verb badge + path
    const m = /^([A-Z]+) (.*)$/.exec(name);
    return m ? [el("span", {"class": "verb"}, m[1]), m[2]] : [name];
  }
  function table(title, rows, cls) {
    if (!rows.length) return el("p", {"class": "muted"}, `${title}: none logged yet.`);
    return el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, title), el("th", {"class": "n"}, "Calls"))),
      el("tbody", {}, rows.map(r => el("tr", {},
        el("td", cls ? {"class": cls} : {}, cls ? endpoint(r.name) : r.name), el("td", {"class": "n"}, r.count)))));
  }
  const tile = (label, value) => el("div", {"class": "tile"}, el("span", {}, label), el("b", {}, value));

  registerTab("Usage", panel => {
    const refresh = el("button", {type: "button", "class": "ghost", onclick: load}, "Refresh");
    const body = el("div", {"class": "u-grid"});
    panel.append(el("div", {"class": "page-head"},
      el("div", {}, el("h1", {}, "Onshape API usage"), el("p", {}, "Calls counted against your annual allocation.")), refresh), body);
    async function load() {
      refresh.disabled = true;
      try {
        const u = await api("/api/usage");
        const fill = el("div", {"class": u.percent >= 90 ? "high" : ""});
        fill.style.width = Math.min(100, u.percent) + "%";
        body.replaceChildren(
          el("div", {"class": "card u-hero"},
            el("div", {"class": "meter-row"},
              el("div", {"class": "meter", role: "img", "aria-label": `${u.percent}% used`}, fill),
              el("span", {"class": "pct"}, u.percent + "%")),
            el("div", {"class": "tiles"},
              tile("Used", u.used), tile("Left", u.left), tile("Allocation", u.allocation), tile("Logged since baseline", u.logged))),
          el("div", {"class": "card"}, table("By command", u.by_purpose)),
          el("div", {"class": "card"}, table("Top endpoints", u.top_endpoints, "ep")));
      } catch (e) { body.replaceChildren(el("p", {"class": "error"}, e.message)); }
      finally { refresh.disabled = false; }
    }
    load();
  });
})();

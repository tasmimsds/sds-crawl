/* Reusable Advanced Filter Builder — one component for Results filtering AND crawl scope.
 * Config: { scopes:[{key,label,on}], fields:[{key,label,type,options}], model, onChange }
 * Produces model = { scopes:{key:bool}, groups:[{mode,field,values:[]}] }.
 * Renders scope pills + group cards (All/Any/None + field + value chips) + live preview.
 */
(function () {
  const MODES = [["all", "All"], ["any", "Any"], ["none", "None"]];
  const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function create(root, cfg) {
    const model = cfg.model && cfg.model.groups
      ? JSON.parse(JSON.stringify(cfg.model))
      : { scopes: {}, groups: [] };
    (cfg.scopes || []).forEach((s) => {
      if (!(s.key in model.scopes)) model.scopes[s.key] = s.on !== false && s.on !== undefined ? s.on : !!s.on;
    });
    if (!model.groups.length) model.groups.push({ mode: "any", field: (cfg.fields[0] || {}).key, values: [] });
    const fieldByKey = Object.fromEntries((cfg.fields || []).map((f) => [f.key, f]));

    function fire() { if (cfg.onChange) cfg.onChange(getModel()); }
    function getModel() { return JSON.parse(JSON.stringify(model)); }

    function groupExpr(g) {
      const f = fieldByKey[g.field];
      const label = f ? f.label : g.field;
      const vals = (g.values || []).map((v) => `${label}:"${esc(v)}"`);
      if (!vals.length) return "";
      const inner = vals.join(g.mode === "all" ? " AND " : " OR ");
      return { none: `NOT (${inner})`, any: `(${inner})`, all: `(${inner})` }[g.mode] || `(${inner})`;
    }
    function overall() {
      let out = "";
      model.groups.forEach((g) => {
        const e = groupExpr(g);
        if (!e) return;
        if (!out) { out = e; return; }
        out += g.mode === "any" ? " OR " + e : g.mode === "none" ? " " + e : " AND " + e;
      });
      const sc = (cfg.scopes || []).filter((s) => model.scopes[s.key]).map((s) => s.label);
      return { expr: out || "(no filter — all items)", scopes: sc };
    }

    function render() {
      const scopesHtml = (cfg.scopes || []).map((s) =>
        `<button type="button" class="fb-pill ${model.scopes[s.key] ? "on" : "off"}" data-scope="${s.key}">
           ${model.scopes[s.key] ? "✓" : "○"} ${esc(s.label)}</button>`).join("");

      const groupsHtml = model.groups.map((g, gi) => {
        const f = fieldByKey[g.field] || {};
        const modeSel = `<select class="fb-mode" data-g="${gi}">` +
          MODES.map(([v, l]) => `<option value="${v}" ${g.mode === v ? "selected" : ""}>${l}</option>`).join("") +
          `</select>`;
        const fieldSel = `<select class="fb-field" data-g="${gi}">` +
          (cfg.fields || []).map((fd) => `<option value="${fd.key}" ${g.field === fd.key ? "selected" : ""}>${esc(fd.label)}</option>`).join("") +
          `</select>`;
        const conn = { all: "AND", any: "OR", none: "NOT" }[g.mode];
        const chips = (g.values || []).map((v, vi) =>
          `<span class="fb-chip">${esc(v)}<b class="fb-x" data-g="${gi}" data-v="${vi}">×</b></span>` +
          (vi < g.values.length - 1 ? `<span class="fb-conn">${conn}</span>` : "")).join("");
        let entry;
        if (f.type === "date") {
          const [s0, e0] = (g.values[0] || "|").split("|");
          entry = `<input type="date" class="fb-date0" data-g="${gi}" value="${esc(s0)}"> –
                   <input type="date" class="fb-date1" data-g="${gi}" value="${esc(e0)}">`;
        } else if (f.type === "enum" && f.options) {
          entry = `<select class="fb-add-sel" data-g="${gi}"><option value="">Add value…</option>` +
            f.options.map((o) => `<option value="${esc(o.value || o)}">${esc(o.label || o)}</option>`).join("") + `</select>`;
        } else {
          entry = `<input type="text" class="fb-add-txt" data-g="${gi}" placeholder="Type value + Enter">
                   <button type="button" class="btn btn-sm fb-add-btn" data-g="${gi}">+</button>`;
        }
        return `<div class="fb-group">
          <div class="fb-group-top">
            <span>Include ${modeSel} of</span> ${fieldSel}
            <button type="button" class="fb-trash" data-g="${gi}" title="Delete group">🗑</button>
          </div>
          <div class="fb-chips">${chips}${chips ? `<span class="fb-conn">${conn}</span>` : ""}${entry}</div>
          <div class="fb-gpreview">${esc(groupExpr(g) || "(empty)")}</div>
        </div>`;
      }).join("");

      const ov = overall();
      root.innerHTML = `
        <div class="fb">
          ${cfg.scopes && cfg.scopes.length ? `<div class="fb-scopes">${scopesHtml}</div>` : ""}
          <div class="fb-groups">${groupsHtml}</div>
          <button type="button" class="btn btn-sm fb-addgroup">+ Add group</button>
          <div class="fb-preview"><span class="muted small">resulting filter:</span><br>
            ${ov.scopes.length ? `<b>${ov.scopes.map(esc).join(" · ")}</b> — ` : ""}${esc(ov.expr)}</div>
        </div>`;
      wire();
    }

    function wire() {
      root.querySelectorAll(".fb-pill").forEach((b) => b.onclick = () => {
        const k = b.dataset.scope; model.scopes[k] = !model.scopes[k]; render(); fire();
      });
      root.querySelectorAll(".fb-mode").forEach((s) => s.onchange = () => {
        model.groups[+s.dataset.g].mode = s.value; render(); fire();
      });
      root.querySelectorAll(".fb-field").forEach((s) => s.onchange = () => {
        const g = model.groups[+s.dataset.g]; g.field = s.value; g.values = []; render(); fire();
      });
      root.querySelectorAll(".fb-x").forEach((x) => x.onclick = () => {
        model.groups[+x.dataset.g].values.splice(+x.dataset.v, 1); render(); fire();
      });
      root.querySelectorAll(".fb-trash").forEach((t) => t.onclick = () => {
        model.groups.splice(+t.dataset.g, 1);
        if (!model.groups.length) model.groups.push({ mode: "any", field: (cfg.fields[0] || {}).key, values: [] });
        render(); fire();
      });
      root.querySelector(".fb-addgroup").onclick = () => {
        model.groups.push({ mode: "any", field: (cfg.fields[0] || {}).key, values: [] }); render(); fire();
      };
      const addVal = (gi, v) => { v = (v || "").trim(); if (!v) return;
        const g = model.groups[gi]; if (!g.values.includes(v)) g.values.push(v); render(); fire(); };
      root.querySelectorAll(".fb-add-txt").forEach((i) => i.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); addVal(+i.dataset.g, i.value); }
      });
      root.querySelectorAll(".fb-add-btn").forEach((b) => b.onclick = () => {
        const i = root.querySelector(`.fb-add-txt[data-g="${b.dataset.g}"]`); addVal(+b.dataset.g, i.value);
      });
      root.querySelectorAll(".fb-add-sel").forEach((s) => s.onchange = () => {
        if (s.value) addVal(+s.dataset.g, s.value);
      });
      const dateChange = (gi) => {
        const s = root.querySelector(`.fb-date0[data-g="${gi}"]`).value;
        const e = root.querySelector(`.fb-date1[data-g="${gi}"]`).value;
        model.groups[gi].values = (s || e) ? [`${s}|${e}`] : []; fire();
        root.querySelector(`.fb-group:nth-child(${gi + 1}) .fb-gpreview`);
      };
      root.querySelectorAll(".fb-date0,.fb-date1").forEach((d) => d.onchange = () => dateChange(+d.dataset.g));
    }

    render();
    return { getModel, setModel(m) { model.scopes = m.scopes || {}; model.groups = m.groups || []; render(); fire(); } };
  }

  window.FilterBuilder = { create };
})();

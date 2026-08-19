// Self-contained helpers (no CDN / framework): switcher, sync, detail rows, tabs.

function switchSite(id) {
  location.href = "/switch?site=" + id + "&next=" + encodeURIComponent(location.pathname + location.search);
}

// tabs
document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  const name = tab.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
});

// expand/collapse issue detail row
function toggleDetail(row) {
  const d = row.nextElementSibling;
  if (d && d.classList.contains("detail")) d.hidden = !d.hidden;
}
window.toggleDetail = toggleDetail;

// ---- one-click sync: the pipeline flow IS the live progress UI ----
function _fmt(n) { return (n == null ? 0 : n).toLocaleString(); }

function applyPipeline(sid, p) {
  const root = document.getElementById("pipe-" + sid);
  if (!root) return;
  root.dataset.jobid = p.job_id || "";
  const stage = (idx, st) => {
    const el = root.querySelector(`.pstage[data-stage="${idx}"]`);
    if (el) el.className = "pstage st-" + st;
    const ar = root.querySelector(`.parrow[data-arrow="${idx}"]`);
    if (ar) ar.className = "parrow st-" + st;
  };
  stage(1, p.s1.status); stage(2, p.s2.status); stage(3, p.s3.status);
  const set = (k, v) => { const e = root.querySelector(`[data-k="${k}"]`); if (e) e.textContent = v; };
  set("s1main", `${_fmt(p.s1.crawled)} / ${_fmt(p.s1.total)} URLs`);
  set("s1err", `${p.s1.errors} errors`);
  set("s2main", `${_fmt(p.s2.read)} pages read`);
  set("s2unread", `${p.s2.unreadable} unreadable`);
  set("s2claims", `${_fmt(p.s2.claims)} claims`);
  set("s3main", `${p.s3.facts} facts checked`);
  set("s3pos", `✓ ${_fmt(p.s3.positive)} positive`);
  set("s3iss", `✗ ${_fmt(p.s3.issues)} issues`);
  set("s3unc", `? ${_fmt(p.s3.unclear)} unclear`);
  const fill = root.querySelector('[data-k="s1fill"]');
  if (fill && p.s1.total) fill.style.width = Math.min(100, Math.round(100 * p.s1.crawled / p.s1.total)) + "%";
}

async function pollPipeline(sid) {
  let p;
  try { p = await (await fetch(`/sites/${sid}/pipeline.json`)).json(); }
  catch (e) { setTimeout(() => pollPipeline(sid), 2500); return; }
  applyPipeline(sid, p);
  if (p.running) { setTimeout(() => pollPipeline(sid), 2000); }
  else { setTimeout(() => location.reload(), 1200); }
}

// clicking Sync opens the run-options step (scope + locale + cost) first
function syncSite(sourceId, onlyChanged) { openRunOptions(sourceId, !!onlyChanged); }

let _runCtx = null;
async function openRunOptions(sid, onlyChanged) {
  const modal = document.getElementById("run-modal");
  const bodyEl = document.getElementById("run-modal-body");
  if (!modal) { return startRunNow(sid, onlyChanged, new URLSearchParams({ only_changed: onlyChanged })); }
  document.getElementById("run-modal-title").textContent = onlyChanged ? "Sync changes — options" : "Start a check — options";
  bodyEl.innerHTML = '<p class="muted small">Loading options…</p>';
  modal.hidden = false;
  let info;
  try { info = await (await fetch(`/sites/${sid}/scope-info.json`)).json(); }
  catch (e) { bodyEl.innerHTML = '<p class="note err">Could not load options.</p>'; return; }
  _runCtx = { sid, onlyChanged, info, crawlModel: null, crawlFb: null };
  bodyEl.innerHTML = buildRunForm(info);
  bodyEl.querySelectorAll("input").forEach((el) => el.addEventListener("change", recalcScope));
  bodyEl.querySelectorAll('input[name="locale_mode"]').forEach((r) =>
    r.addEventListener("change", syncCrawlAdvanced));
  syncCrawlAdvanced();
  recalcScope();
}
function crawlFields(info) {
  return [
    { key: "locale", label: "Locale", type: "enum", options: (info.locales || []).map((l) => l.code) },
    { key: "section", label: "Section/path", type: "enum", options: (info.sections || []).map((s) => s.code) },
    { key: "url_contains", label: "URL contains", type: "text" },
    { key: "url_not_contains", label: "URL does NOT contain", type: "text" },
    { key: "changed", label: "Changed since last sync", type: "enum", options: ["yes", "no"] },
    { key: "lastmod", label: "Lastmod date", type: "date" },
  ];
}
function syncCrawlAdvanced() {
  const mount = document.getElementById("crawl-fb-mount");
  if (!mount || !_runCtx) return;
  const adv = document.querySelector('input[name="locale_mode"][value="advanced"]:checked');
  mount.hidden = !adv;
  if (adv && !_runCtx.crawlFb && window.FilterBuilder) {
    const saved = _runCtx.info.saved.locale;
    _runCtx.crawlFb = FilterBuilder.create(mount, {
      scopes: [], fields: crawlFields(_runCtx.info),
      model: (saved && saved.mode === "advanced" && saved.filter) ? saved.filter : undefined,
      onChange: (m) => { _runCtx.crawlModel = m; recalcScope(); },
    });
    _runCtx.crawlModel = _runCtx.crawlFb.getModel();
  }
}
function closeRun() { const m = document.getElementById("run-modal"); if (m) m.hidden = true; }
window.closeRun = closeRun;

function buildRunForm(info) {
  const loc = info.saved.locale;
  const chk = (b) => (b ? "checked" : "");
  let h = `<div class="run-sec">
    <div class="run-h">Fact check crawl</div>
    <p class="muted small" style="margin:0">Every crawl reads page content and runs fact matching
      (including FAQ). Choose which URLs to cover below.</p>
  </div>`;
  if (info.has_locale) {
    const lm = loc.mode || "all";
    h += `<div class="run-sec">
      <div class="run-h">Which parts of the site?</div>
      <label class="opt ${lm === 'all' ? 'sel' : ''}"><input type="radio" name="locale_mode" value="all" ${chk(lm === 'all')}>
        <span><b>All locales</b> <span class="muted small">(${info.total.toLocaleString()} URLs)</span></span></label>
      <label class="opt ${lm === 'english' ? 'sel' : ''}"><input type="radio" name="locale_mode" value="english" ${chk(lm === 'english')}>
        <span><b>⭐ English only</b> <span class="muted small">${info.english.map((c) => '/' + c).join(', ') || '(none detected)'} + root</span></span></label>
      <label class="opt ${lm === 'custom' ? 'sel' : ''}"><input type="radio" name="locale_mode" value="custom" ${chk(lm === 'custom')}>
        <span><b>Custom</b></span></label>
      <div class="locale-grid" id="locale-grid">
        <div class="lg-actions"><button type="button" class="btn btn-sm" onclick="localeAll(1)">Select all</button>
          <button type="button" class="btn btn-sm" onclick="localeAll(0)">Clear</button></div>`;
    const sel = new Set(loc.locales || []);
    const preselect = lm !== "custom"; // when not custom, boxes mirror the preset for preview only
    info.locales.forEach((l) => {
      const on = lm === "custom" ? sel.has(l.code) : (lm === "all" || (lm === "english" && (info.english.includes(l.code) || l.code === "(root)")));
      h += `<label class="chk"><input type="checkbox" name="locales" value="${l.code}" ${on ? "checked" : ""}> ${l.code} <span class="muted small">${l.count.toLocaleString()} URLs</span></label>`;
    });
    h += `</div>
      <label class="opt ${lm === 'advanced' ? 'sel' : ''}"><input type="radio" name="locale_mode" value="advanced" ${chk(lm === 'advanced')}>
        <span><b>Advanced</b> <span class="muted small">— precise group query (locale, section, URL patterns…)</span></span></label>
      <div id="crawl-fb-mount" ${lm === 'advanced' ? '' : 'hidden'}></div>
      </div>`;
  }
  h += `<div class="run-preview" id="run-preview"></div>
    <div class="form-actions">
      <button class="btn btn-primary" onclick="doStartRun()">Start check</button>
      <button class="btn" onclick="closeRun()">Cancel</button>
    </div>`;
  return h;
}

function localeAll(on) {
  document.querySelectorAll('#locale-grid input[name="locales"]').forEach((c) => { c.checked = !!on; });
  const cu = document.querySelector('input[name="locale_mode"][value="custom"]');
  if (cu) cu.checked = true;
  recalcScope();
}
window.localeAll = localeAll;

function _selectedLocaleMode() {
  const r = document.querySelector('input[name="locale_mode"]:checked');
  return r ? r.value : "all";
}
function recalcScope() {
  const ctx = _runCtx; if (!ctx) return;
  const info = ctx.info;
  // keep .sel highlight + custom radio in sync
  document.querySelectorAll("#run-modal .opt").forEach((o) => {
    const i = o.querySelector("input"); o.classList.toggle("sel", i && i.checked);
  });
  const preview = document.getElementById("run-preview"); if (!preview) return;
  const full = false;  // fact-check only — no full/SEO mode
  // advanced crawl scope -> live count from the backend (URL-selection filter)
  if (info.has_locale && _selectedLocaleMode() === "advanced") {
    const m = ctx.crawlModel || { groups: [] };
    fetch(`/sites/${ctx.sid}/scope-count.json?filter=` + encodeURIComponent(JSON.stringify(m)))
      .then((r) => r.json()).then((j) => {
        const rate = full ? info.rate.full : info.rate.fact;
        const usd = j.selected * rate;
        const mins = Math.max(1, Math.round(j.selected * info.sec_per_page / info.concurrency / 60));
        preview.innerHTML = `<b>This crawl will fetch ${j.selected.toLocaleString()} of ${j.total.toLocaleString()} URLs</b>`
          + ` · ~$${usd.toFixed(2)}, ~${mins} min <span class="muted small">(estimate)</span>`;
      }).catch(() => {});
    return;
  }
  let selUrls, llmPages;
  if (!info.has_locale) { selUrls = info.total; llmPages = info.english_llm_total || info.total; }
  else {
    const lm = _selectedLocaleMode();
    if (lm === "all") { selUrls = info.total; llmPages = info.english_llm_total; }
    else if (lm === "english") {
      selUrls = info.locales.filter((l) => info.english.includes(l.code) || l.code === "(root)")
        .reduce((a, l) => a + l.count, 0);
      llmPages = selUrls;
    } else {
      const checked = new Set([...document.querySelectorAll('#locale-grid input:checked')].map((c) => c.value));
      selUrls = info.locales.filter((l) => checked.has(l.code)).reduce((a, l) => a + l.count, 0);
      llmPages = info.locales.filter((l) => checked.has(l.code) && (info.english.includes(l.code) || l.code === "(root)")).reduce((a, l) => a + l.count, 0);
    }
  }
  const rate = full ? info.rate.full : info.rate.fact;
  const usd = (llmPages * rate);
  const mins = Math.max(1, Math.round(selUrls * info.sec_per_page / info.concurrency / 60));
  preview.innerHTML = `<b>Selected scope:</b> ${selUrls.toLocaleString()} of ${info.total.toLocaleString()} URLs`
    + ` · <b>Estimated:</b> ~${selUrls.toLocaleString()} pages, ~$${usd.toFixed(2)}, ~${mins} min`
    + ` <span class="muted small">(estimate)</span>`;
}
window.recalcScope = recalcScope;

async function doStartRun() {
  const ctx = _runCtx; if (!ctx) return;
  const body = new URLSearchParams();
  body.set("only_changed", ctx.onlyChanged ? "true" : "false");
  // fact-check only tool — analysis scope is always fact matching; only URL scope varies
  const lm = document.querySelector('input[name="locale_mode"]:checked');
  body.set("locale_mode", lm ? lm.value : "all");
  if (lm && lm.value === "custom") {
    document.querySelectorAll('#locale-grid input[name="locales"]:checked').forEach((c) => body.append("locales", c.value));
  }
  if (lm && lm.value === "advanced") {
    body.set("crawl_filter", JSON.stringify(ctx.crawlModel || { groups: [] }));
  }
  await startRunNow(ctx.sid, ctx.onlyChanged, body);
  closeRun();
}
window.doStartRun = doStartRun;

async function startRunNow(sid, onlyChanged, body) {
  const state = document.getElementById("state-" + sid);
  if (state) { state.textContent = "Syncing…"; state.className = "status status-syncing"; }
  const res = await fetch(`/sites/${sid}/sync`, { method: "POST", body });
  if (res.status === 409) { const j = await res.json(); alertBanner(j.error || "A run is already in progress."); return; }
  const { job_id } = await res.json();
  const cancelBtn = document.getElementById("cancel-" + sid);
  if (cancelBtn) { cancelBtn.style.display = ""; cancelBtn.setAttribute("onclick", `cancelSync(${job_id}, ${sid})`); }
  pollPipeline(sid);
}
window.syncSite = syncSite;
window.openRunOptions = openRunOptions;
window.applyPipeline = applyPipeline;
window.pollPipeline = pollPipeline;

// resume live polling for any pipeline already running on page load
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".pipeline").forEach((pl) => {
    if (!pl.querySelector(".pstage.st-running")) return;
    if (pl.classList.contains("ext-pipeline")) {
      switchFlow(pl.dataset.source, "external");  // surface the active external run
      pollExternalPipeline(pl.dataset.source);
    } else {
      pollPipeline(pl.dataset.source);
    }
  });
});

// ---- editable search-term chips ----
function initChips() {
  document.querySelectorAll(".chips-edit").forEach((box) => {
    const hidden = box.querySelector('input[name="search_terms"]');
    const list = box.querySelector(".chips-list");
    const add = box.querySelector(".chip-add");
    // newline-delimited so terms may contain commas (e.g. 17,000,000)
    const terms = () => hidden.value.split("\n").map((t) => t.trim()).filter(Boolean);
    const write = (arr) => { hidden.value = arr.join("\n"); paint(); };
    const paint = () => {
      list.innerHTML = "";
      terms().forEach((t, i) => {
        const chip = document.createElement("span");
        chip.className = "chip term-chip";
        chip.innerHTML = esc(t) + ' <b class="x">×</b>';
        chip.querySelector(".x").onclick = () => { const a = terms(); a.splice(i, 1); write(a); };
        list.appendChild(chip);
      });
    };
    if (add) add.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); const v = add.value.trim();
        if (v) { const a = terms(); a.push(v); write(a); } add.value = ""; }
    });
    paint();
  });
}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

// ---- results: verdict filter + mark issue ----
function filterVerdict(btn, v) {
  document.querySelectorAll(".vf").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".vrow").forEach((r) => {
    r.style.display = (v === "all" || r.dataset.verdict === v) ? "" : "none";
  });
}
window.filterVerdict = filterVerdict;

// ---- Results Advanced Filter Builder wiring ----
let _fbInstance = null, _fbModel = null;
function openFilterBuilder() {
  const modal = document.getElementById("filter-modal");
  if (!modal || !window.FilterBuilder) return;
  modal.hidden = false;
  _fbInstance = FilterBuilder.create(document.getElementById("fb-mount"), {
    scopes: window.FINDINGS_SCOPES, fields: window.FINDINGS_FIELDS,
    model: window.FINDINGS_INITIAL || undefined,
    onChange: (m) => { _fbModel = m; fbCount(m); },
  });
  _fbModel = _fbInstance.getModel();
  fbCount(_fbModel);
}
function closeFilterModal() { const m = document.getElementById("filter-modal"); if (m) m.hidden = true; }
async function fbCount(m) {
  const el = document.getElementById("fb-count");
  try {
    const j = await (await fetch("/filters/count.json?filter=" + encodeURIComponent(JSON.stringify(m)))).json();
    if (el) el.textContent = `Matches: ${j.count} findings` +
      (j.external ? ` (${j.internal} internal · ${j.external} external)` : "");
  } catch (e) { /* ignore */ }
}
function applyFilter() {
  location.href = "/results?filter=" + encodeURIComponent(JSON.stringify(_fbModel || {}));
}
function fbClear() { if (_fbInstance) _fbInstance.setModel({ scopes: {}, groups: [] }); }
async function fbSave() {
  const inp = document.getElementById("fb-save-name");
  if (!inp) return;
  if (inp.hidden) { inp.hidden = false; inp.focus(); return; }   // no browser prompt
  const name = inp.value.trim();
  if (!name) { inp.focus(); return; }
  const body = new URLSearchParams({ name, context: "findings", model: JSON.stringify(_fbModel || {}) });
  const r = await fetch("/filters/save", { method: "POST", body });
  if (r.ok) { alertBanner(`Saved filter "${name}".`); inp.hidden = true; inp.value = ""; }
  else { alertBanner("Save failed."); }
}
window.openFilterBuilder = openFilterBuilder; window.closeFilterModal = closeFilterModal;
window.applyFilter = applyFilter; window.fbClear = fbClear; window.fbSave = fbSave;
// show live match count in the filters bar when a filter is active
document.addEventListener("DOMContentLoaded", () => {
  if (window.FINDINGS_INITIAL) {
    const el = document.getElementById("filter-count");
    fetch("/filters/count.json?filter=" + encodeURIComponent(JSON.stringify(window.FINDINGS_INITIAL)))
      .then((r) => r.json()).then((j) => { if (el) el.textContent = `${j.count} findings match`; })
      .catch(() => {});
  }
});

function filterVerdictBtn(v) {
  const btn = document.querySelector(`.vf[onclick*="'${v}'"]`);
  if (btn) btn.click();
}
function filterVerdictAll() { filterVerdictBtn("all"); }
window.filterVerdictBtn = filterVerdictBtn;
window.filterVerdictAll = filterVerdictAll;

async function markIssue(id, status, btn) {
  const body = new URLSearchParams({ status });
  await fetch(`/issues/${id}/mark`, { method: "POST", body });
  const card = btn.closest(".match-card");
  if (card) { card.style.opacity = ".5"; btn.parentElement.innerHTML = `<span class="muted small">Marked ${status}</span>`; }
}
window.markIssue = markIssue;

// ---- sync then continue the fact check ----
async function syncThenRun(sourceId) {
  const msg = document.getElementById("sync-msg");
  if (msg) msg.textContent = "Syncing…";
  const res = await fetch(`/sites/${sourceId}/sync`, { method: "POST", body: new URLSearchParams({ only_changed: "false" }) });
  const { job_id } = await res.json();
  const poll = async () => {
    const j = await (await fetch(`/jobs/${job_id}.json`)).json();
    if (msg) msg.textContent = j.message || j.status;
    if (j.status === "running" || j.status === "queued") setTimeout(poll, 2000);
    else document.getElementById("after-sync").submit();
  };
  poll();
}
window.syncThenRun = syncThenRun;

document.addEventListener("DOMContentLoaded", initChips);

// ---- findings management (recheck / edit / mark-incorrect / delete) ----
function toggleBox(id) { const el = document.getElementById(id); if (el) el.hidden = !el.hidden; }
window.toggleBox = toggleBox;

async function fcRecheck(id) {
  const st = document.querySelector(".st-" + id);
  if (st) st.textContent = "rechecking…";
  const j = await (await fetch(`/issues/${id}/recheck`, { method: "POST" })).json();
  if (st) st.textContent = j.status || "?";
  const row = document.getElementById("row-" + id);
  if (row && (j.status === "fixed" || j.status === "unverifiable")) row.style.opacity = ".5";
  alertBanner(j.message || "Rechecked");
}
window.fcRecheck = fcRecheck;

async function fcDelete(id) {
  if (!confirm("Delete this finding? It stays suppressed on re-scans unless the page text changes.")) return;
  await fetch(`/issues/${id}/delete`, { method: "POST" });
  removeRow(id);
}
window.fcDelete = fcDelete;

// "show full context" — pulls the wider paragraph from the STORED page body (no re-crawl)
async function showContext(ev, id) {
  ev.stopPropagation();
  const box = document.getElementById("ctx-" + id);
  const btn = ev.currentTarget;
  if (!box) return;
  if (!box.hidden) { box.hidden = true; btn.textContent = "show full context ▾"; return; }
  if (!box.dataset.loaded) {
    box.textContent = "loading…";
    box.hidden = false;
    try {
      const j = await (await fetch(`/issues/${id}/context`)).json();
      const ctx = (j && j.context) || "(no stored context)";
      if (j && j.matched) {
        const i = ctx.indexOf(j.matched);
        if (i >= 0) {
          box.textContent = "";
          box.append(document.createTextNode(ctx.slice(0, i)));
          const mk = document.createElement("mark");
          mk.textContent = j.matched;
          box.append(mk, document.createTextNode(ctx.slice(i + j.matched.length)));
        } else { box.textContent = ctx; }
      } else { box.textContent = ctx; }
      box.dataset.loaded = "1";
    } catch (e) { box.textContent = "failed to load context"; }
  } else {
    box.hidden = false;
  }
  btn.textContent = "hide full context ▴";
}
window.showContext = showContext;

// false-positive modal
function openFp(btn) {
  document.getElementById("fp-id").value = btn.dataset.id;
  document.getElementById("fp-reason").value = "";
  document.getElementById("fp-flag").checked = false;
  document.getElementById("fp-phrase").value = btn.dataset.phrase || "";
  document.getElementById("fp-modal").hidden = false;
}
function closeFp() { document.getElementById("fp-modal").hidden = true; }
async function submitFp() {
  const id = document.getElementById("fp-id").value;
  const body = new URLSearchParams({
    reason: document.getElementById("fp-reason").value,
    phrase: document.getElementById("fp-phrase").value,
  });
  if (document.getElementById("fp-flag").checked) body.set("dont_flag", "1");
  const j = await (await fetch(`/issues/${id}/false-positive`, { method: "POST", body })).json();
  closeFp();
  alertBanner(j.allowed_added_to
    ? `Marked incorrect. Won't flag this pattern again (rule: ${j.allowed_added_to}).`
    : "Marked as incorrect.");
  removeRow(id);
}
window.openFp = openFp; window.closeFp = closeFp; window.submitFp = submitFp;

async function extRecheck(fid, btn) {
  btn.textContent = "rechecking…";
  const j = await (await fetch(`/external/${fid}/recheck`, { method: "POST" })).json();
  alertBanner(j.message || "Rechecked");
  const card = btn.closest(".match-card");
  if (card && j.status && j.status !== "open") card.style.opacity = ".5";
  btn.textContent = "↻ Recheck";
}
window.extRecheck = extRecheck;

function removeRow(id) {
  const row = document.getElementById("row-" + id);
  if (row) { const d = row.nextElementSibling; row.remove(); if (d && d.classList.contains("detail")) d.remove(); }
}

async function recheckSection(category) {
  const msg = document.getElementById("bulk-msg");
  const status = (new URLSearchParams(location.search)).get("status") || "open";
  if (msg) msg.textContent = "Rechecking… (this can take a while)";
  const body = new URLSearchParams({ category, status });
  const j = await (await fetch("/issues/recheck-section", { method: "POST", body })).json();
  if (msg) msg.textContent = `Checked ${j.checked}: ${j.fixed} fixed, ${j.still_open} still open, ${j.unverifiable} unverifiable.`;
  setTimeout(() => location.reload(), 1500);
}
window.recheckSection = recheckSection;

async function extMark(id, action, btn) {
  if (action === "delete" && !confirm("Delete this external finding?")) return;
  await fetch(`/external/${id}/mark`, { method: "POST", body: new URLSearchParams({ action }) });
  const card = btn.closest(".match-card");
  if (card) { card.style.opacity = ".4"; btn.parentElement.innerHTML = `<span class="muted small">${action.replace('_',' ')}</span>`; }
}
window.extMark = extMark;

function alertBanner(text) {
  let b = document.getElementById("_banner");
  if (!b) { b = document.createElement("div"); b.id = "_banner"; b.className = "toast"; document.body.appendChild(b); }
  b.textContent = text; b.style.display = "block";
  clearTimeout(window._bt); window._bt = setTimeout(() => { b.style.display = "none"; }, 3500);
}

// ---- dashboard flow tabs: Internal | External ----
function switchFlow(sid, flow) {
  document.querySelectorAll(`.flow-panel[data-source="${sid}"]`).forEach((p) => {
    p.hidden = p.dataset.flow !== flow;
  });
  document.querySelectorAll(`.flow-tabs[data-source="${sid}"] .flow-tab`).forEach((t) => {
    t.classList.toggle("active", t.dataset.flow === flow);
  });
}
window.switchFlow = switchFlow;

// ---- EXTERNAL pipeline: DISCOVER → FETCH → SCOPE → READ → FACT MATCH ----
function applyExternalPipeline(sid, p) {
  const root = document.getElementById("extpipe-" + sid);
  if (!root) return;
  root.dataset.jobid = p.job_id || "";
  const stage = (idx, st) => {
    const el = root.querySelector(`.pstage[data-stage="${idx}"]`);
    if (el) el.className = "pstage st-" + st;
    const ar = root.querySelector(`.parrow[data-arrow="${idx}"]`);
    if (ar) ar.className = "parrow st-" + st;
  };
  stage(1, p.s1.status); stage(2, p.s2.status); stage(3, p.s3.status);
  stage(4, p.s4.status); stage(5, p.s5.status);
  const set = (k, v) => { const e = root.querySelector(`[data-k="${k}"]`); if (e) e.textContent = v; };
  set("e1main", `${_fmt(p.s1.total)} sources`);
  set("e1bl", `🔗 ${_fmt(p.s1.true_backlinks)} backlinks · ${_fmt(p.s1.true_domains)} domains`);
  set("e1men", `💬 ${_fmt(p.s1.search_mentions)} search mentions · ${_fmt(p.s1.linked)} linked / ${_fmt(p.s1.unlinked)} unlinked`);
  set("e1def", p.s1.deferred ? `⏸ ${_fmt(p.s1.deferred)} deferred (over cap)` : "");
  set("e2main", `${_fmt(p.s2.ok)} fetched`);
  set("e2block", `${p.s2.blocked} blocked`);
  set("e2err", `${p.s2.errored} errored`);
  set("e3main", `${_fmt(p.s3.kept)} / ${_fmt(p.s3.total)} about brand`);
  set("e3disc", `${p.s3.discarded} discarded (competitor/generic)`);
  set("e4main", `${_fmt(p.s4.claims)} claims`);
  set("e5main", `${_fmt(p.s5.positive + p.s5.issues + p.s5.unclear + (p.s5.general || 0))} sorted`);
  set("e5iss", `✗ ${_fmt(p.s5.issues)} mismatch → Issues`);
  set("e5pos", `✓ ${_fmt(p.s5.positive)} correct`);
  set("e5gen", `📋 ${_fmt(p.s5.general || 0)} general facts`);
}

async function pollExternalPipeline(sid) {
  let p;
  try { p = await (await fetch(`/sites/${sid}/external-pipeline.json`)).json(); }
  catch (e) { setTimeout(() => pollExternalPipeline(sid), 2500); return; }
  applyExternalPipeline(sid, p);
  if (p.running) { setTimeout(() => pollExternalPipeline(sid), 2000); }
  else { setTimeout(() => location.reload(), 1200); }
}
window.pollExternalPipeline = pollExternalPipeline;

// one-click external check (dashboard): cost preview → run → animate stages 1→5
async function runExternal(sid) {
  // legacy fact-check page path (no sid) keeps its old behavior
  if (sid === undefined) return runExternalLegacy();
  let info;
  try { info = await (await fetch(`/sites/${sid}/external-scope.json`)).json(); }
  catch (e) { info = null; }
  if (info && !info.has_brand && info.pages_scoped === 0) {
    alert("No brand profile or external sources yet. Add External Sources first (Fact Check page).");
    return;
  }
  if (info) {
    const lines = [
      `Run external check for ${info.brand || "this brand"}?`,
      ``,
      `• ${info.pending_to_fetch} new page(s) to fetch` +
        (info.already_fetched ? ` + ${info.already_fetched} already fetched` : ""),
      info.discover_enabled ? `• web discovery ON (adds candidates to approve)` : `• web discovery off`,
      `• SCOPE + FACT MATCH use the LLM — est. cost ≈ $${info.est_cost_usd}`,
    ];
    if (!confirm(lines.join("\n"))) return;
  }
  switchFlow(sid, "external");
  const btn = document.getElementById("extrun-" + sid);
  if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
  const res = await fetch("/external/run", { method: "POST", body: new URLSearchParams({ source_id: sid }) });
  if (res.status === 409) { const j = await res.json(); alert(j.error); if (btn) { btn.disabled = false; btn.textContent = "Run external check"; } return; }
  const { error } = await res.json();
  if (error) { alert(error); if (btn) { btn.disabled = false; btn.textContent = "Run external check"; } return; }
  pollExternalPipeline(sid);
}
window.runExternal = runExternal;

// legacy fact-check-page runner (uses ext-prog/ext-fill/ext-msg elements)
async function runExternalLegacy() {
  const prog = document.getElementById("ext-prog");
  const fill = document.getElementById("ext-fill");
  const msg = document.getElementById("ext-msg");
  if (prog) prog.style.display = "block";
  if (msg) msg.textContent = "Starting…";
  const res = await fetch("/external/run", { method: "POST", body: new URLSearchParams({}) });
  if (res.status === 409) { const j = await res.json(); alert(j.error); return; }
  const { job_id, error } = await res.json();
  if (error) { if (msg) msg.textContent = error; return; }
  const poll = async () => {
    const j = await (await fetch(`/jobs/${job_id}.json`)).json();
    if (fill) fill.style.width = (j.pct || 0) + "%";
    if (msg) msg.textContent = j.message || j.status;
    if (j.status === "running" || j.status === "queued") setTimeout(poll, 2000);
    else setTimeout(() => location.reload(), 1200);
  };
  poll();
}

// ---- General Facts review actions ----
function gfTab(ev, which) {
  ev.preventDefault();
  document.querySelectorAll("#panel-general,#panel-backlinks").forEach((p) => {
    p.hidden = p.id !== "panel-" + which;
  });
  document.querySelectorAll(".tabs .tab").forEach((t) => t.classList.remove("active"));
  ev.currentTarget.classList.add("active");
}
window.gfTab = gfTab;

async function gfFlag(id, value) {
  await fetch(`/general-facts/${id}/update`, { method: "POST", body: new URLSearchParams({ needs_change: value }) });
  const sel = document.querySelector(`#gf-${id} .gf-flag`);
  if (sel) sel.className = "gf-flag flag-" + value;
}
window.gfFlag = gfFlag;

async function gfNote(id, note) {
  await fetch(`/general-facts/${id}/update`, { method: "POST", body: new URLSearchParams({ note }) });
}
window.gfNote = gfNote;

async function gfDismiss(id) {
  if (!confirm("Dismiss this item? It won't show in the review list.")) return;
  await fetch(`/general-facts/${id}/dismiss`, { method: "POST" });
  const row = document.getElementById("gf-" + id);
  if (row) row.remove();
}
window.gfDismiss = gfDismiss;

async function gfCreateIssue(id) {
  if (!confirm("This statement is wrong → create an external issue from it?")) return;
  const j = await (await fetch(`/general-facts/${id}/create-issue`, { method: "POST" })).json();
  if (j.ok) { alertBanner("Issue created — see Results & Issues (External)."); const r = document.getElementById("gf-" + id); if (r) r.style.opacity = ".5"; }
}
window.gfCreateIssue = gfCreateIssue;

async function gfPromote(id) {
  const val = prompt("Promote to a tracked fact rule.\nEnter the CORRECT value for this fact (optional):", "");
  if (val === null) return;
  const j = await (await fetch(`/general-facts/${id}/promote-fact`, { method: "POST", body: new URLSearchParams({ current_value: val }) })).json();
  if (j.ok) { alertBanner("Promoted to a fact rule (" + j.fact_id + ") — see Facts Library."); const r = document.getElementById("gf-" + id); if (r) r.style.opacity = ".5"; }
}
window.gfPromote = gfPromote;

async function cancelSync(jobId, sourceId) {
  if (!jobId) return;
  await fetch(`/jobs/${jobId}/cancel`, { method: "POST" });
  alertBanner("Cancelling…");
}
window.cancelSync = cancelSync;

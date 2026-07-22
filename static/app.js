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

// ---- one-click sync with staged live progress ----
async function syncSite(sourceId, onlyChanged) {
  const prog = document.getElementById("prog-" + sourceId);
  const fill = document.getElementById("fill-" + sourceId);
  const msg = document.getElementById("msg-" + sourceId);
  const state = document.getElementById("state-" + sourceId);
  const cancelBtn = document.getElementById("cancel-" + sourceId);
  if (prog) prog.style.display = "block";
  if (msg) msg.textContent = "Starting…";
  if (state) { state.textContent = "Syncing…"; state.className = "status status-syncing"; }

  const body = new URLSearchParams({ only_changed: onlyChanged ? "true" : "false" });
  const res = await fetch(`/sites/${sourceId}/sync`, { method: "POST", body });
  if (res.status === 409) { const j = await res.json(); alert(j.error); return; }
  const { job_id } = await res.json();
  if (cancelBtn) { cancelBtn.style.display = ""; cancelBtn.setAttribute("onclick", `cancelSync(${job_id}, ${sourceId})`); }

  const poll = async () => {
    const j = await (await fetch(`/jobs/${job_id}.json`)).json();
    if (fill) fill.style.width = (j.pct || 0) + "%";
    if (msg) msg.textContent = j.message || j.status;
    if (j.status === "running" || j.status === "queued") {
      setTimeout(poll, 2000);
    } else {
      if (msg) msg.textContent = j.error ? "Error: " + j.error : (j.message || "Done");
      setTimeout(() => location.reload(), 1200);
    }
  };
  poll();
}
window.syncSite = syncSite;

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

// ---- external check (discover -> fetch -> scope -> fact-check) ----
async function runExternal() {
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
window.runExternal = runExternal;

async function cancelSync(jobId, sourceId) {
  if (!jobId) return;
  await fetch(`/jobs/${jobId}/cancel`, { method: "POST" });
  const msg = document.getElementById("msg-" + sourceId);
  if (msg) msg.textContent = "Cancelling…";
}
window.cancelSync = cancelSync;

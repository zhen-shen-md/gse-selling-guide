import {
  AGENCIES, CHANGE_TYPES, SUBSTANTIVE_TYPES, TRANSACTION_LABELS, changeDate, crosswalkFor, deterministicSummary,
  filterChanges, prepareSections, pricingAttributeOptions, pricingGridCatalog, pricingScenario, retrieveEvidence,
  reverseCrosswalk, searchSections,
} from "./core.mjs";

const state = { meta: null, sections: null, changes: null, crosswalk: null, pricing: null, ai: { enabled: false } };
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const VIEWS = ["ask", "search", "sidebyside", "changes", "pricing", "about"];

function esc(value = "") {
  return String(value ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[c]);
}
function safeUrl(url) {
  return /^https:\/\//.test(url || "") ? esc(url) : null;
}
function link(url, label) {
  const href = safeUrl(url);
  return href ? `<a class="source-link" href="${href}" target="_blank" rel="noreferrer">${esc(label)} ↗</a>` : "";
}
function notice(el, message = "", kind = "info") {
  el.textContent = message;
  el.classList.toggle("hidden", !message);
  el.classList.toggle("error", kind === "error");
}
async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Could not load ${path} (${response.status})`);
  return response.json();
}
const fmtDate = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : "—");

// ---------- data loading (lazy) ----------
async function loadSections() {
  if (!state.sections) {
    const [f, r] = await Promise.all([getJson("data/sections-fannie.json"), getJson("data/sections-freddie.json")]);
    state.sections = { fannie: prepareSections(f, "fannie"), freddie: prepareSections(r, "freddie") };
    state.sectionIndex = new Map();
    for (const s of [...state.sections.fannie, ...state.sections.freddie]) {
      const key = `${s.agencyKey}:${s.id}`;
      if (!state.sectionIndex.has(key) || s.st === "current") state.sectionIndex.set(key, s);
    }
  }
  return state.sections;
}
async function loadChanges() { return (state.changes ??= await getJson("data/changes.json")); }
async function loadCrosswalk() { return (state.crosswalk ??= await getJson("data/crosswalk.json")); }
async function loadPricing() { return (state.pricing ??= await getJson("data/pricing.json")); }
const section = (agencyKey, id) => state.sectionIndex?.get(`${agencyKey}:${id}`);

function switchView(name, push = true) {
  if (!VIEWS.includes(name)) return;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (push) history.replaceState(null, "", `#${name}`);
  if (name === "changes") renderChanges();
  if (name === "pricing") initPricing();
  window.scrollTo({ top: 0 });
}

// ---------- Ask & compare ----------
function evidenceCard(row) {
  const dates = row.status === "future" ? `Effective ${fmtDate(row.effectiveDate)}` : row.revisionDate ? `Revised ${fmtDate(row.revisionDate)}` : "";
  return `<article class="evidence-item">
    <div class="evidence-meta">${esc(row.hierarchy)}</div>
    <h3>${esc(row.id)} · ${esc(row.title)}</h3>
    <div class="evidence-meta">${esc(dates)}</div>
    <p>${esc(row.snippet)}</p>
    <div class="link-row">${link(row.officialLink, "Official page")}${link(row.pdfLink, `PDF p. ${row.page}`)}
      <button type="button" class="text-button" data-open="${esc(row.agencyKey)}:${esc(row.id)}">Side-by-side →</button></div>
  </article>`;
}
function renderSummary(summary, target) {
  const rows = summary.differences.map((d) => `<tr><td><strong>${esc(d.dimension)}</strong></td><td>${esc(d.fannie)}</td><td>${esc(d.freddie)}</td><td>${esc(d.impact)}</td></tr>`).join("");
  const list = (items) => `<ul class="summary-list">${(items || []).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
  target.innerHTML = `<div class="summary-topline"><span class="eyebrow">Comparison summary</span><span class="summary-mode">${summary.mode === "ai" ? `AI · ${esc(summary.model)}` : "Evidence only (no AI)"}</span></div>
    <h2>${esc(summary.headline)}</h2><p class="summary-lead">${esc(summary.summary)}</p>
    <div class="summary-section"><h3>Differences</h3><table class="difference-table"><thead><tr><th>Dimension</th><th>Fannie Mae</th><th>Freddie Mac</th><th>Why it matters</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="summary-section"><h3>Similarities</h3>${list(summary.similarities)}</div>
    <div class="summary-section"><h3>Caveats</h3>${list([...(summary.implementationNotes || []), ...(summary.caveats || [])])}</div>`;
  target.classList.remove("hidden");
}
async function ask(question) {
  const note = $("#ask-notice");
  const button = $("#ask-form button[type=submit]");
  button.disabled = true;
  notice(note, "Retrieving evidence from both guides…");
  try {
    const sections = await loadSections();
    const f = retrieveEvidence(sections.fannie, question, AGENCIES.fannie);
    const r = retrieveEvidence(sections.freddie, question, AGENCIES.freddie);
    if (!f.evidence.length || !r.evidence.length) throw new Error("No evidence found in both guides. Try a more specific mortgage-policy phrase.");
    $("#ask-evidence").innerHTML = `<div><h2 class="column-title fannie">Fannie Mae <small>Searched as: ${esc(f.retrievalQuery)}</small></h2>${f.evidence.map(evidenceCard).join("")}</div>
      <div><h2 class="column-title freddie">Freddie Mac <small>Searched as: ${esc(r.retrievalQuery)}</small></h2>${r.evidence.map(evidenceCard).join("")}</div>`;
    $("#ask-evidence").classList.remove("hidden");
    const withText = (row) => ({ ...row, text: section(row.agencyKey, row.id)?.x ?? "" });
    renderSummary(deterministicSummary(withText(f.evidence[0]), withText(r.evidence[0]), question), $("#ask-summary"));
    notice(note, state.ai.enabled ? "Generating AI summary…" : "");
    if (state.ai.enabled) {
      const response = await fetch("api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "AI request failed");
      renderSummary(payload.summary, $("#ask-summary"));
      notice(note, payload.summary.aiError ? "The AI request failed; showing the evidence-only summary." : "", payload.summary.aiError ? "error" : "info");
    }
  } catch (error) {
    notice(note, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

// ---------- Search ----------
async function runSearch() {
  const note = $("#search-notice");
  notice(note, "Searching…");
  try {
    const sections = await loadSections();
    const q = $("#search-query").value.trim();
    const agency = $("#search-agency").value;
    const includeFuture = $("#search-future").checked;
    const pool = agency === "both" ? [...sections.fannie, ...sections.freddie] : sections[agency];
    const rows = searchSections(pool, q, { limit: 25, includeFuture });
    $("#search-results").innerHTML = rows.map((row) => `<article class="search-result">
      <div><span class="search-agency ${esc(row.agencyKey)}">${esc(row.agency)}</span>
        ${row.status === "future" ? `<span class="badge upcoming">Effective ${esc(fmtDate(row.effectiveDate))}</span>` : ""}
        <div class="evidence-meta">${row.revisionDate ? `Revised ${esc(fmtDate(row.revisionDate))}` : ""}</div></div>
      <div><h2>${esc(row.id)} · ${esc(row.title)}</h2><div class="evidence-meta">${esc(row.hierarchy)}</div><p>${esc(row.snippet)}</p></div>
      <div class="result-links">${link(row.officialLink, "Official page")}${link(row.pdfLink, `PDF p. ${row.page}`)}
        <button type="button" class="text-button" data-open="${esc(row.agencyKey)}:${esc(row.id)}">Side-by-side →</button></div>
    </article>`).join("") || '<div class="notice">No matching sections.</div>';
    notice(note, "");
  } catch (error) {
    notice(note, error.message, "error");
  }
}

// ---------- Side-by-side ----------
function sectionPanel(s, agencyKey) {
  if (!s) return `<article class="sbs-panel empty"><p>Select a counterpart.</p></article>`;
  const dates = s.st === "future" ? `Future effective ${fmtDate(s.eff)}` : s.rev ? `Revised ${fmtDate(s.rev)}` : "";
  return `<article class="sbs-panel ${agencyKey}">
    <header><span class="agency-kicker">${esc(s.agency)}</span><h2>${esc(s.id)} · ${esc(s.t)}</h2>
      <div class="evidence-meta">${esc(s.h)}${dates ? ` · ${esc(dates)}` : ""}</div>
      <div class="link-row">${link(s.off, "Official page")}${link(s.src, `PDF p. ${s.p}`)}</div></header>
    <div class="section-text">${esc(reflow(s.x))}</div></article>`;
}
async function openSideBySide(agencyKey, id, counterpartId = null) {
  const note = $("#sbs-notice");
  switchView("sidebyside", false);
  history.replaceState(null, "", `#sidebyside/${agencyKey}/${encodeURIComponent(id)}${counterpartId ? `/${encodeURIComponent(counterpartId)}` : ""}`);
  $("#sbs-id").value = id;
  try {
    await loadSections();
    const cw = await loadCrosswalk();
    const s = section(agencyKey, id);
    if (!s) throw new Error(`No ${AGENCIES[agencyKey]} section ${id} in the current edition.`);
    let options;
    if (agencyKey === "fannie") {
      const { curated, candidates } = crosswalkFor(cw, id);
      const curatedIds = curated.flatMap((p) => p.freddie_section_ids);
      options = [...curatedIds.map((fid) => ({ id: fid, label: "Reviewed pair", conf: "curated" })),
        ...candidates.filter((c) => !curatedIds.includes(c.freddieId)).map((c) => ({ id: c.freddieId, label: `Candidate #${c.rank}`, conf: c.confidence, score: c.score }))];
    } else {
      options = reverseCrosswalk(cw, id).map((c) => ({ id: c.fannieId, label: `Candidate (rank ${c.rank})`, conf: c.confidence, score: c.score }));
    }
    const otherKey = agencyKey === "fannie" ? "freddie" : "fannie";
    const chosen = counterpartId || options[0]?.id;
    const other = chosen ? section(otherKey, chosen) : null;
    const left = agencyKey === "fannie" ? s : other;
    const right = agencyKey === "fannie" ? other : s;
    const chips = options.map((o) => {
      const t = section(otherKey, o.id);
      return `<button type="button" class="candidate ${o.id === chosen ? "active" : ""} conf-${esc(o.conf)}" data-pick="${esc(agencyKey)}:${esc(id)}:${esc(o.id)}">
        <strong>${esc(o.id)}</strong> ${esc(t?.t ?? "")}<small>${esc(o.label)}${o.score != null ? ` · ${o.conf} · ${o.score.toFixed(2)}` : ""}</small></button>`;
    }).join("");
    let summary = "";
    if (left && right) {
      const sum = deterministicSummary({ title: left.t, text: left.x }, { title: right.t, text: right.x }, `${left.id} vs ${right.id}`);
      summary = `<div class="sbs-signals"><strong>${esc(sum.headline)}</strong> ${sum.differences.map((d) => `<span><b>${esc(d.dimension)}:</b> Fannie ${esc(d.fannie)} · Freddie ${esc(d.freddie)}</span>`).join("")}</div>`;
    }
    $("#sbs-result").innerHTML = `<div class="candidate-list"><span class="eyebrow">Likely counterparts in ${esc(AGENCIES[otherKey])}</span>${chips || "<p>No candidates.</p>"}
      <p class="form-note">Candidates are relevance suggestions, not legal equivalence.</p></div>${summary}
      <div class="sbs-grid">${sectionPanel(left, "fannie")}${sectionPanel(right, "freddie")}</div>`;
    notice(note, "");
  } catch (error) {
    notice(note, error.message, "error");
  }
}
async function submitSideBySide() {
  await loadSections();
  const id = $("#sbs-id").value.trim().toUpperCase().split(" ")[0];
  const agencyKey = /^\d{4}\.\d+$/.test(id) ? "freddie" : "fannie";
  openSideBySide(agencyKey, id);
}

// ---------- What's new ----------
let changesInitialized = false;
async function renderChanges() {
  const changes = await loadChanges();
  if (!changesInitialized) {
    changesInitialized = true;
    $("#chg-type").innerHTML = `<option value="in_effect">In effect now (substantive)</option><option value="upcoming_all">Upcoming (future-effective)</option><option value="substantive">All substantive</option><option value="all">All, incl. editorial/layout</option>`
      + Object.entries(CHANGE_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    const months = new Set();
    const anns = new Set();
    for (const a of Object.values(changes)) for (const c of a.records) { if (changeDate(c)) months.add(changeDate(c).slice(0, 7)); if (c.ann) anns.add(c.ann.id); }
    $("#chg-month").innerHTML += [...months].sort().reverse().map((m) => `<option value="${m}">${m}</option>`).join("");
    $("#chg-ann").innerHTML += [...anns].sort().map((a) => `<option value="${a}">${a}</option>`).join("");
    const substantive = (counts) => Object.entries(counts).filter(([t]) => SUBSTANTIVE_TYPES.has(t)).reduce((s, [, n]) => s + n, 0);
    const periodLabel = (p) => `${fmtDate(p.old)} → ${fmtDate(p.new)}`;
    $("#chg-period").innerHTML += Object.entries(changes).flatMap(([key, a]) => a.periods.map((p) =>
      `<option value="${esc(p.id)}">${esc(AGENCIES[key])}: ${esc(periodLabel(p))}</option>`)).join("");
    $("#change-stats").innerHTML = Object.entries(changes).map(([key, a]) => {
      const total = a.periods.reduce((s, p) => s + substantive(p.counts), 0);
      const rows = a.periods.map((p) => `<button type="button" class="period-row" data-period="${esc(p.id)}" data-agency="${esc(key)}">
        <span>${esc(periodLabel(p))}</span><b>${substantive(p.counts)}</b></button>`).join("");
      return `<article class="stat-card"><span class="agency-kicker">${esc(AGENCIES[key])}</span>
        <strong>${total}</strong><span>substantive changes · ${esc(fmtDate(a.old))} → ${esc(fmtDate(a.new))}</span>
        <div class="period-rows">${rows}</div></article>`;
    }).join("");
    $("#changes-lead").textContent = `Section-level changes, edition by edition: Fannie Mae ${fmtDate(changes.fannie?.old)} → ${fmtDate(changes.fannie?.new)} (monthly) and Freddie Mac ${fmtDate(changes.freddie?.old)} → ${fmtDate(changes.freddie?.new)}. Each change links to the announcement that introduced it. "Upcoming" lists Freddie sections already published with a future effective date.`;
  }
  const agency = $("#chg-agency").value;
  const typeValue = $("#chg-type").value;
  const upcoming = new Set(["upcoming", "upcoming_revised"]);
  const types = typeValue === "in_effect" ? new Set([...SUBSTANTIVE_TYPES].filter((t) => !upcoming.has(t)))
    : typeValue === "upcoming_all" ? upcoming
      : typeValue === "substantive" ? SUBSTANTIVE_TYPES : typeValue === "all" ? null : new Set([typeValue]);
  const rows = [];
  for (const [key, a] of Object.entries(changes)) {
    if (agency !== "both" && agency !== key) continue;
    rows.push(...filterChanges(a.records, { types, month: $("#chg-month").value, text: $("#chg-text").value, announcement: $("#chg-ann").value, period: $("#chg-period").value })
      .map((c) => ({ ...c, agencyKey: key, periodLabel: a.periods.length > 1 ? `${fmtDate(c.period.split("__")[0])} → ${fmtDate(c.period.split("__")[1])} edition` : "" })));
  }
  // Changes in effect: newest first. Upcoming changes: soonest effective date first.
  rows.sort((a, b) => (upcoming.has(a.type) - upcoming.has(b.type))
    || (upcoming.has(a.type) ? changeDate(a).localeCompare(changeDate(b)) : changeDate(b).localeCompare(changeDate(a))));
  $("#changes-count").textContent = `${rows.length} changes`;
  $("#changes-list").innerHTML = rows.slice(0, 300).map(changeItem).join("") + (rows.length > 300 ? `<div class="notice">Showing the first 300; narrow the filters to see more.</div>` : "");
}
function changeItem(c) {
  const ann = c.ann ? `<a class="chip" href="${safeUrl(c.ann.url) ?? "#"}" target="_blank" rel="noreferrer">${esc(c.ann.id)}</a>` : "";
  const ids = c.osid && c.nsid && c.osid !== c.nsid ? `${esc(c.osid)} → ${esc(c.nsid)}` : esc(c.sid);
  const when = c.type.startsWith("upcoming") ? `Effective ${fmtDate(c.eff)}` : c.nrev ? `${fmtDate(c.orev)} → ${fmtDate(c.nrev)}` : "";
  const diffList = (items, cls) => items?.length ? `<ul class="diff-list ${cls}">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "";
  const succ = c.succ?.length ? `<p class="evidence-meta">Content now in: ${c.succ.map((s) => `${esc(s.section_id)} ${esc(s.section_title)} (${Math.round(s.share_of_old_text * 100)}% of old text)`).join("; ")}</p>` : "";
  const retitle = c.otitle && c.otitle !== c.title ? `<p class="evidence-meta">Previously titled “${esc(c.otitle)}”.</p>` : "";
  return `<details class="change-item type-${esc(c.type)}">
    <summary><span class="badge t-${esc(c.type)}">${esc(CHANGE_TYPES[c.type] ?? c.type)}</span>
      <span class="agency-dot ${esc(c.agencyKey)}">${esc(AGENCIES[c.agencyKey])}</span>
      <strong>${ids} · ${esc(c.title)}</strong><span class="change-when">${esc(when)}</span>${ann}</summary>
    <div class="change-body">
      <div class="evidence-meta">${esc(c.h)}${c.periodLabel ? ` · Found in the ${esc(c.periodLabel)}` : ""}</div>${retitle}${succ}
      ${c.sa != null ? `<p class="evidence-meta">${c.sa} passages added, ${c.sr} removed${c.sim != null ? ` · ${Math.round(c.sim * 100)}% similar` : ""}${c.more ? " · list truncated" : ""}</p>` : ""}
      <div class="diff-grid">${c.rem?.length ? `<div><h4>Removed or replaced</h4>${diffList(c.rem, "removed")}</div>` : ""}${c.add?.length ? `<div><h4>Added or new wording</h4>${diffList(c.add, "added")}</div>` : ""}</div>
      <div class="link-row">${link(c.off, "Official page")}${link(c.src, "PDF page")}
        ${c.nsid ? `<button type="button" class="text-button" data-open="${esc(c.agencyKey)}:${esc(c.nsid)}">Side-by-side →</button>` : ""}
        ${state.ai.enabled && (c.add?.length || c.rem?.length) ? `<button type="button" class="secondary-button" data-explain="${esc(c.cid)}">Explain with AI</button>` : ""}</div>
      <div class="ai-explanation" id="exp-${esc(c.cid).replace(/[^a-zA-Z0-9]/g, "_")}"></div>
    </div></details>`;
}
async function explain(cid, button) {
  const target = document.getElementById(`exp-${cid.replace(/[^a-zA-Z0-9]/g, "_")}`);
  button.disabled = true;
  target.textContent = "Asking AI…";
  try {
    const response = await fetch("api/explain-change", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changeId: cid }) });
    const { explanation: e, error } = await response.json();
    if (!response.ok) throw new Error(error);
    target.innerHTML = `<strong>${esc(e.headline)}</strong><ul>${e.whatChanged.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
      <p><b>Who is affected:</b> ${esc(e.whoIsAffected)}</p><p class="form-note">${esc((e.caveats || []).join(" "))} ${e.mode === "ai" ? `(AI · ${esc(e.model)})` : ""}</p>`;
  } catch (error) {
    target.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// ---------- Pricing ----------
let pricingInitialized = false;
async function initPricing() {
  if (pricingInitialized) return;
  pricingInitialized = true;
  const p = await loadPricing();
  state.grids = pricingGridCatalog(p.records);
  $("#pricing-headline").textContent = p.insights.headline;
  $("#pricing-insights").innerHTML = p.insights.findings.map((f) => `<article class="insight"><strong>${esc(f.finding)}</strong><p>${esc(f.evidence)}</p></article>`).join("");
  $("#attribute-options").innerHTML = pricingAttributeOptions(p.records).map((o) => `<label class="attribute-option"><input type="checkbox" value="${esc(o.key)}"><span>${esc(o.label)}</span></label>`).join("");
  renderGrids();
  renderProvisions();
}
function runPricing() {
  const attributes = $$("#attribute-options input:checked").map((i) => i.value);
  const r = pricingScenario(state.pricing.records, { transaction: $("#pricing-transaction").value, score: Number($("#pricing-score").value), ltv: Number($("#pricing-ltv").value), attributes });
  const card = (agency) => {
    const d = r.agencies[agency];
    const rows = d.components.map((c) => `<div class="fee-row"><span>${esc(c.label)} ${link(c.link, `p. ${c.page}`)}</span><strong>${c.feePercent.toFixed(3)}%</strong></div>`).join("");
    const missing = d.unmatchedAttributes.length ? `<p class="form-note">No published cell for: ${esc(d.unmatchedAttributes.join(", "))}</p>` : "";
    return `<article class="agency-pricing"><header><strong>${agency}</strong><span>${d.found ? "Grid cell found" : "No grid cell"}</span></header>${rows}${missing}
      <div class="fee-row total-row"><span>Uncapped total</span><strong>${d.uncappedFeePercent == null ? "N/A" : `${d.uncappedFeePercent.toFixed(3)}%`}</strong></div></article>`;
  };
  const delta = r.deltaFannieMinusFreddie;
  $("#pricing-result").innerHTML = `<div class="pricing-summary"><div><span class="eyebrow">Scenario</span><h2>${esc(TRANSACTION_LABELS[r.inputs.transaction])}</h2></div>
    <div class="delta-display"><strong>${delta == null ? "N/A" : `${delta.toFixed(3)}%`}</strong><span>Fannie minus Freddie</span></div></div>
    <div class="agency-pricing-grid">${card(AGENCIES.fannie)}${card(AGENCIES.freddie)}</div>
    <ul class="pricing-limitations">${r.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
}
function matrix(segment, agency) {
  const d = segment.agencies[agency];
  const other = segment.agencies[agency === AGENCIES.fannie ? AGENCIES.freddie : AGENCIES.fannie];
  const val = new Map(d.cells.map((c) => [`${c.scoreBand}::${c.ltvBand}`, c.feePercent]));
  const oth = new Map(other.cells.map((c) => [`${c.scoreBand}::${c.ltvBand}`, c.feePercent]));
  const head = segment.ltvBands.map((b) => `<th scope="col">${esc(b.label)}</th>`).join("");
  const body = segment.scoreBands.map((s) => `<tr><th scope="row">${esc(s.label)}</th>${segment.ltvBands.map((b) => {
    const k = `${s.key}::${b.key}`;
    const v = val.get(k);
    const differs = v != null && oth.get(k) != null && Math.abs(v - oth.get(k)) > 1e-4;
    return `<td class="matrix-value${differs ? " differs" : ""}">${v == null ? "—" : `${v.toFixed(3)}%`}</td>`;
  }).join("")}</tr>`).join("");
  return `<article class="agency-matrix"><header><div><span class="agency-kicker">${esc(agency)}</span><small>${d.effectiveDate ? `Effective ${esc(fmtDate(d.effectiveDate))}` : "No matching grid"}</small></div>${link(d.link, `p. ${d.page}`)}</header>
    ${d.cells.length ? `<div class="matrix-scroll"><table class="pricing-matrix"><thead><tr><th>Score / LTV</th>${head}</tr></thead><tbody>${body}</tbody></table></div>` : '<div class="matrix-empty">No corresponding cells.</div>'}</article>`;
}
function renderGrids() {
  const fam = $("#grid-family-filter").value;
  const tx = $("#grid-transaction-filter").value;
  const segs = state.grids.segments.filter((s) => (fam === "all" || s.family === fam) && (tx === "all" || s.transaction === tx));
  $("#pricing-grid-count").textContent = `${segs.length} of ${state.grids.segments.length} grids`;
  $("#pricing-grid-catalog").innerHTML = segs.map((s) => `<details class="matrix-segment"><summary><span><span class="segment-family">${esc(s.familyLabel)}</span><strong>${esc(s.title)}</strong></span>
    <span class="segment-shape">${s.scoreBands.length} × ${s.ltvBands.length}</span></summary><div class="agency-matrix-grid">${matrix(s, AGENCIES.fannie)}${matrix(s, AGENCIES.freddie)}</div></details>`).join("");
}
function renderProvisions() {
  const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  $("#pricing-provisions").innerHTML = Object.values(AGENCIES).map((agency) => {
    const rows = state.grids.provisions[agency].map((p) => {
      const value = p.amount_usd != null ? money.format(p.amount_usd) : `${Number(p.cap_percent).toFixed(3)}% cap`;
      const details = [p.condition, p.special_feature_code ? `SFC ${p.special_feature_code}` : null].filter(Boolean).join(" · ");
      return `<div class="provision-row"><div><span>${p.record_type === "fixed_dollar_credit" ? "Dollar credit" : "Waiver / zero cap"}</span><strong>${esc(p.product_feature)}</strong>${details ? `<p>${esc(details)}</p>` : ""}</div>
        <div><b>${esc(value)}</b>${link(p.source_url, `p. ${p.source_page}`)}</div></div>`;
    }).join("");
    return `<article class="provision-card"><header><span class="agency-kicker">${esc(agency)}</span></header>${rows}</article>`;
  }).join("");
}

// ---------- About ----------
function renderAbout() {
  const m = state.meta;
  const kinds = { selling_guide: "Selling guide", pricing: "Pricing", eligibility_reference: "Eligibility reference" };
  $("#source-grid").innerHTML = m.sources.map((s) => `<article class="source-card"><span class="source-type">${esc(kinds[s.kind] ?? s.kind)}</span>
    <h2>${esc(s.agency)}</h2><p>${esc(s.document_id)}${s.bulletin ? ` (Bulletin ${esc(s.bulletin)})` : ""}</p>
    ${s.url ? link(s.url, "Official source") : '<p class="form-note">Archived edition (no public URL); derived data kept for change history.</p>'}</article>`).join("")
    + `<article class="source-card"><span class="source-type">Announcements</span><h2>Fannie Mae</h2><p>${m.announcements.map((a) => `${link(a.url, `${a.id} (${a.date})`)}`).join(" ")}</p></article>`;
}

// ---------- startup ----------
async function init() {
  try {
    state.meta = await getJson("data/meta.json");
    const e = state.meta.editions;
    $("#metric-fannie").textContent = fmtDate(e.fannie.edition_date);
    $("#metric-freddie").textContent = fmtDate(e.freddie.edition_date);
    $("#kb-status").textContent = `Validation: ${state.meta.validation.status} · data ${state.meta.generated_at.slice(0, 10)}`;
    renderAbout();
  } catch (error) {
    $("#kb-status").textContent = "Data unavailable";
    notice($("#ask-notice"), `${error.message}. Run python pipeline/build_site_data.py`, "error");
  }
  try {
    const status = await getJson("api/status");
    state.ai = status.ai;
    $("#ai-mode").textContent = status.ai.enabled ? `AI · ${status.ai.model}` : "Evidence mode";
  } catch { /* static hosting: no server, evidence mode */ }
  loadSections().then(() => {
    $("#sbs-options").innerHTML = [...state.sections.fannie, ...state.sections.freddie].filter((s) => s.st === "current" && /\d/.test(s.id))
      .map((s) => `<option value="${esc(s.id)}">${esc(s.t)}</option>`).join("");
  }).catch(() => {});
  routeFromHash();
}

// Deep links: #ask/<question>, #search/<query>, #sidebyside/<fannie|freddie>/<id>[/<counterpart id>]
function routeFromHash() {
  const [view, ...rest] = location.hash.slice(1).split("/").map(decodeURIComponent);
  if (view === "ask" && rest[0]) { switchView("ask", false); $("#ask-query").value = rest.join("/"); ask(rest.join("/")); return; }
  if (view === "search" && rest[0]) { switchView("search", false); $("#search-query").value = rest.join("/"); runSearch(); return; }
  if (view === "sidebyside" && rest[1]) { openSideBySide(rest[0], rest[1], rest[2] || null); return; }
  switchView(VIEWS.includes(view) ? view : "ask", false);
}

$$(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#ask-query").value.trim()); });
$$(".suggestions button").forEach((b) => b.addEventListener("click", () => { $("#ask-query").value = b.dataset.query; ask(b.dataset.query); }));
$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
$("#sbs-form").addEventListener("submit", (e) => { e.preventDefault(); submitSideBySide(); });
$("#changes-form").addEventListener("input", () => renderChanges());
$("#changes-form").addEventListener("submit", (e) => e.preventDefault());
$("#pricing-form").addEventListener("submit", (e) => { e.preventDefault(); runPricing(); });
$("#grid-family-filter").addEventListener("change", renderGrids);
$("#grid-transaction-filter").addEventListener("change", renderGrids);
document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) { const [agency, id] = open.dataset.open.split(":"); openSideBySide(agency, id); }
  const pick = e.target.closest("[data-pick]");
  if (pick) { const [agency, id, other] = pick.dataset.pick.split(":"); openSideBySide(agency, id, other); }
  const exp = e.target.closest("[data-explain]");
  if (exp) explain(exp.dataset.explain, exp);
  const period = e.target.closest("[data-period]");
  if (period) {
    $("#chg-agency").value = period.dataset.agency;
    $("#chg-period").value = period.dataset.period;
    renderChanges();
  }
});
window.addEventListener("hashchange", routeFromHash);

init();

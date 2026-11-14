// Shared logic for the browser (static GitHub Pages site) and the optional Node server.
// No Node or DOM APIs here, so both environments run identical search and comparison code.

export const AGENCIES = { fannie: "Fannie Mae", freddie: "Freddie Mac" };

const STOP_WORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "in", "is", "it",
  "loan", "loans", "mortgage", "mortgages", "of", "on", "or", "that", "the", "their", "to", "what", "when",
  "which", "with", "differ", "difference", "differences", "compare", "between", "fannie", "freddie", "mae", "mac",
  "this", "these", "contains", "including", "information", "topic", "section", "uses", "additional",
]);

/** Re-flow PDF hard line breaks into paragraphs for reading (keeps list items and headings on their own lines). */
export function reflow(text = "") {
  return text.replace(/([^\n.:;?!])\n(?=[a-z(,])/g, "$1 ");
}

const MOJIBAKE = [["â€™", "’"], ["â€˜", "‘"], ["â€œ", "“"], ["â€\u009d", "”"], ["â€“", "–"], ["â€”", "—"],
  ["â€¦", "…"], ["Â®", "®"], ["Â©", "©"], ["Â", ""]];

export function repairText(value = "") {
  if (!/[ÃÂâ€]/.test(value)) return value;
  let out = value;
  for (const [broken, fixed] of MOJIBAKE) out = out.replaceAll(broken, fixed);
  return out;
}

export function tokenize(value = "") {
  return repairText(value).toLowerCase().replace(/[^a-z0-9%.-]+/g, " ").split(/\s+/)
    .map((t) => t.replace(/^[.-]+|[.-]+$/g, ""))
    .filter((t) => t.length > 1 && !STOP_WORDS.has(t));
}

const LIMITED_RE = /\b(?:limited[\s-]+cash[\s-]*out|no[\s-]+cash[\s-]*out|rate[\s-]+and[\s-]+term)(?:\s+refinance)?\b/i;

/** Map agency-specific terminology (Fannie "limited cash-out" = Freddie "no cash-out"). */
export function queryForAgency(question, agency) {
  const clean = repairText(question).trim();
  if (!LIMITED_RE.test(clean)) return clean;
  const rest = clean.replace(new RegExp(LIMITED_RE.source, "gi"), " ").replace(/\s+/g, " ").trim();
  return `${agency === AGENCIES.fannie ? "limited cash-out refinance" : "no cash-out refinance"} ${rest}`.trim();
}

export function excerpt(text, terms, length = 360) {
  const clean = repairText(text).replace(/\s+/g, " ").trim();
  const lower = clean.toLowerCase();
  const hits = terms.map((t) => lower.indexOf(t)).filter((i) => i >= 0);
  const center = hits.length ? Math.min(...hits) : 0;
  const start = Math.max(0, center - Math.floor(length * 0.3));
  return `${start > 0 ? "…" : ""}${clean.slice(start, start + length)}${start + length < clean.length ? "…" : ""}`;
}

/** Prepare compact site sections (from build_site_data.py) for repeated searching. */
export function prepareSections(rows, agencyKey) {
  return rows.map((r) => ({
    ...r,
    agency: AGENCIES[agencyKey],
    agencyKey,
    _title: repairText(`${r.id} ${r.t}`).toLowerCase(),
    _body: repairText(r.x).toLowerCase(),
  }));
}

export function sourceLink(section) {
  return section.off || section.src || null;
}

export function searchSections(sections, query, { limit = 10, includeFuture = false } = {}) {
  const terms = tokenize(query);
  const phrase = repairText(query).toLowerCase().trim();
  if (!terms.length) return [];
  const results = [];
  for (const row of sections) {
    if (!includeFuture && row.st === "future") continue;
    let score = 0;
    let matched = 0;
    for (const term of terms) {
      const titleHits = row._title.split(term).length - 1;
      const bodyHits = Math.min(10, row._body.split(term).length - 1);
      if (titleHits || bodyHits) matched += 1;
      score += titleHits * 9 + Math.log1p(bodyHits) * 3;
      if (row.id.toLowerCase() === term) score += 60;
    }
    if (phrase.length > 3) {
      if (row._title.includes(phrase)) score += 30;
      if (row._body.includes(phrase)) score += 12;
    }
    score *= 0.4 + 0.6 * (matched / terms.length) ** 2;
    if (score > 0) results.push({ row, score });
  }
  results.sort((a, b) => b.score - a.score || a.row.id.localeCompare(b.row.id));
  return results.slice(0, Math.max(1, Math.min(limit, 50))).map(({ row, score }) => ({
    id: row.id, rid: row.rid, agency: row.agency, agencyKey: row.agencyKey, title: repairText(row.t),
    status: row.st, revisionDate: row.rev, effectiveDate: row.eff, hierarchy: row.h, page: row.p,
    link: sourceLink(row), pdfLink: row.src, officialLink: row.off,
    snippet: excerpt(row.x, terms), relevance: Math.round(score * 10) / 10,
  }));
}

function uniqueSections(rows, limit) {
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    out.push(row);
    if (out.length >= limit) break;
  }
  return out;
}

/** Retrieve the best sections from one agency for a plain-language question. */
export function retrieveEvidence(sections, question, agency, limit = 4) {
  const retrievalQuery = queryForAgency(question, agency);
  const results = searchSections(sections, retrievalQuery, { limit: 20 });
  let evidence = uniqueSections(results, limit);
  if (LIMITED_RE.test(question)) {
    const phrase = agency === AGENCIES.fannie ? "limited cash-out" : "no cash-out";
    const exact = results.filter((r) => r.title.toLowerCase().includes(phrase));
    if (exact.length) evidence = uniqueSections(exact, limit);
  }
  return { retrievalQuery, evidence };
}

function modalSignals(text) {
  const patterns = ["must not", "must", "may not", "may", "required", "not permitted", "ineligible", "eligible"];
  return patterns.filter((s) => new RegExp(`\\b${s.replace(" ", "\\s+")}\\b`, "i").test(text));
}

function numericSignals(text) {
  return [...new Set(text.match(/(?:\$[\d,.]+|\b\d+(?:\.\d+)?%|\b\d{1,3}\s*(?:days?|months?|years?)\b)/gi) || [])].slice(0, 12);
}

/** Evidence-only comparison used when no AI key is configured (and as the AI fallback). */
export function deterministicSummary(fannie, freddie, query) {
  const fText = repairText(fannie.text || "");
  const rText = repairText(freddie.text || "");
  const fTerms = new Set(tokenize(`${fannie.title} ${fText}`));
  const rTerms = new Set(tokenize(`${freddie.title} ${rText}`));
  const shared = [...fTerms].filter((t) => rTerms.has(t));
  const union = new Set([...fTerms, ...rTerms]);
  const overlap = union.size ? shared.length / union.size : 0;
  const fNum = numericSignals(fText);
  const rNum = numericSignals(rText);
  const sharedNum = fNum.filter((v) => rNum.includes(v));
  const differences = [];
  const fOnly = fNum.filter((v) => !rNum.includes(v));
  const rOnly = rNum.filter((v) => !fNum.includes(v));
  if (fOnly.length || rOnly.length) {
    differences.push({
      dimension: "Thresholds and time periods",
      fannie: fOnly.join(", ") || "No unique numeric signal in the excerpt.",
      freddie: rOnly.join(", ") || "No unique numeric signal in the excerpt.",
      impact: "Check each number in context; automated extraction cannot tell whether differently stated thresholds are equivalent.",
    });
  }
  const fModal = modalSignals(fText);
  const rModal = modalSignals(rText);
  if (fModal.join("|") !== rModal.join("|")) {
    differences.push({
      dimension: "Obligation language",
      fannie: fModal.join(", ") || "No modal verbs detected.",
      freddie: rModal.join(", ") || "No modal verbs detected.",
      impact: "Different obligation verbs can change operational requirements; read the cited sections.",
    });
  }
  if (!differences.length) {
    differences.push({
      dimension: "Policy expression",
      fannie: fannie.title, freddie: freddie.title,
      impact: "No numeric or obligation-language difference was detected automatically; that does not establish equivalence.",
    });
  }
  return {
    mode: "deterministic", model: null,
    headline: overlap > 0.42 ? "The selected sections cover substantially overlapping concepts." : "The selected sections overlap only partly.",
    overallRelationship: overlap > 0.42 ? "partial_overlap" : "insufficient_evidence",
    summary: `Evidence-only comparison for “${query}”. Vocabulary overlap is ${Math.round(overlap * 100)}%. Read the cited sections to confirm scope, exceptions, and effective dates.`,
    similarities: [
      sharedNum.length ? `Both excerpts contain: ${sharedNum.join(", ")}.` : "No shared numeric threshold detected.",
      shared.length ? `Shared vocabulary includes ${shared.slice(0, 8).join(", ")}.` : "Little distinctive shared vocabulary.",
    ],
    differences,
    implementationNotes: ["Machine-selected sections are research leads, not a legal-equivalence determination."],
    caveats: ["Generated from text signals without AI. Eligibility, exceptions, and effective dates require source review."],
  };
}

/** Crosswalk candidates for a Fannie section, curated pairs first. */
export function crosswalkFor(crosswalk, fannieId) {
  const curated = crosswalk.curated.filter((p) => p.fannie_section_id === fannieId);
  const candidates = (crosswalk.byFannie[fannieId] || []).map(([freddieId, rank, score, confidence, review]) => ({
    freddieId, rank, score, confidence, review,
  }));
  return { curated, candidates };
}

/** Fannie topics whose candidate lists include a given Freddie section. */
export function reverseCrosswalk(crosswalk, freddieId) {
  const out = [];
  for (const [fannieId, rows] of Object.entries(crosswalk.byFannie)) {
    for (const [fid, rank, score, confidence, review] of rows) {
      if (fid === freddieId) out.push({ fannieId, rank, score, confidence, review });
    }
  }
  return out.sort((a, b) => b.score - a.score).slice(0, 8);
}

// ---------- Pricing ----------

export const TRANSACTION_LABELS = {
  purchase: "Purchase",
  rate_term_refinance: "Fannie limited cash-out / Freddie no cash-out",
  cash_out_refinance: "Cash-out refinance",
};

function ltvMatches(row, ltv) {
  return (row.ltv_min_exclusive == null || ltv > row.ltv_min_exclusive) && (row.ltv_max_inclusive == null || ltv <= row.ltv_max_inclusive);
}

function scoreMatches(row, score) {
  return row.score_band === "all_scores"
    || ((row.score_min_inclusive == null || score >= row.score_min_inclusive) && (row.score_max_exclusive == null || score < row.score_max_exclusive));
}

export function pricingScenario(records, { transaction, score, ltv, attributes = [] }) {
  const result = { inputs: { transaction, score, ltv, attributes }, agencies: {} };
  for (const agency of Object.values(AGENCIES)) {
    const base = records.find((r) => r.agency === agency && r.pricing_family === "base" && r.transaction_category === transaction && scoreMatches(r, score) && ltvMatches(r, ltv));
    const components = base ? [{ component: "base", label: "Base grid", feePercent: base.fee_percent, page: base.source_page, link: base.source_url }] : [];
    const unmatchedAttributes = [];
    for (const attribute of attributes) {
      const row = records.find((r) => r.agency === agency && r.pricing_family === "attribute" && r.transaction_category === transaction && r.attribute_key === attribute && ltvMatches(r, ltv));
      if (row) components.push({ component: attribute, label: row.attribute_label, feePercent: row.fee_percent, page: row.source_page, link: row.source_url });
      else unmatchedAttributes.push(attribute);
    }
    result.agencies[agency] = {
      found: Boolean(base),
      components,
      uncappedFeePercent: base ? Math.round(components.reduce((s, c) => s + c.feePercent, 0) * 1000) / 1000 : null,
      unmatchedAttributes,
    };
  }
  const a = result.agencies[AGENCIES.fannie].uncappedFeePercent;
  const b = result.agencies[AGENCIES.freddie].uncappedFeePercent;
  result.deltaFannieMinusFreddie = a == null || b == null ? null : Math.round((a - b) * 1000) / 1000;
  result.limitations = [
    "Uncapped matrix lookup only; not a price quote or eligibility decision.",
    "Waivers, fixed-dollar credits, mortgage-insurance options, term exclusions, and contract pricing are not applied.",
  ];
  return result;
}

function scoreBandLabel(r) {
  if (r.score_band === "all_scores") return "All scores";
  if (r.score_min_inclusive != null && r.score_max_exclusive == null) return `≥ ${r.score_min_inclusive}`;
  if (r.score_min_inclusive == null && r.score_max_exclusive != null) return `< ${r.score_max_exclusive}`;
  return `${r.score_min_inclusive}–${r.score_max_exclusive - 1}`;
}

function ltvBandLabel(r) {
  if (r.ltv_min_exclusive == null) return `≤ ${r.ltv_max_inclusive}%`;
  if (r.ltv_max_inclusive == null) return `> ${r.ltv_min_exclusive}%`;
  return `${r.ltv_min_exclusive}–${r.ltv_max_inclusive}%`;
}

export function pricingAttributeOptions(records) {
  const map = new Map();
  for (const r of records) {
    if (r.pricing_family === "attribute" && !map.has(r.attribute_key)) map.set(r.attribute_key, r.attribute_label);
  }
  return [...map.entries()].map(([key, label]) => ({ key, label }));
}

export function pricingGridCatalog(records) {
  const grid = records.filter((r) => r.record_type === "price_adjustment_grid_cell");
  const groups = new Map();
  for (const r of grid) {
    const key = [r.pricing_family, r.transaction_category ?? "all", r.attribute_key ?? "none"].join("::");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const familyOrder = { base: 0, attribute: 1, mortgage_insurance: 2 };
  const txOrder = { purchase: 0, rate_term_refinance: 1, cash_out_refinance: 2, all: 3 };
  const segments = [...groups.entries()].map(([id, rows]) => {
    const first = rows[0];
    const scoreBands = [...new Map(rows.map((r) => [r.score_band, { key: r.score_band, label: scoreBandLabel(r), min: r.score_min_inclusive }])).values()]
      .sort((a, b) => (b.min ?? -1) - (a.min ?? -1));
    const ltvBands = [...new Map(rows.map((r) => [r.ltv_band, { key: r.ltv_band, label: ltvBandLabel(r), max: r.ltv_max_inclusive }])).values()]
      .sort((a, b) => (a.max ?? Infinity) - (b.max ?? Infinity));
    const agencies = {};
    for (const agency of Object.values(AGENCIES)) {
      const ar = rows.filter((r) => r.agency === agency);
      agencies[agency] = {
        effectiveDate: ar[0]?.effective_date ?? null, page: ar[0]?.source_page ?? null, link: ar[0]?.source_url ?? null,
        cells: ar.map((r) => ({ scoreBand: r.score_band, ltvBand: r.ltv_band, feePercent: r.fee_percent })),
      };
    }
    const family = first.pricing_family;
    const transaction = first.transaction_category ?? "all";
    const label = rows.find((r) => r.attribute_label)?.attribute_label;
    const title = family === "base" ? `${TRANSACTION_LABELS[transaction]} · Base grid`
      : family === "mortgage_insurance" ? "Minimum / custom mortgage insurance coverage"
        : `${label ?? first.attribute_key} · ${TRANSACTION_LABELS[transaction]}`;
    return { id, family, transaction, title, scoreBands, ltvBands, agencies,
      familyLabel: { base: "Base grids", attribute: "Loan attributes", mortgage_insurance: "Mortgage insurance" }[family] };
  }).sort((a, b) => (familyOrder[a.family] - familyOrder[b.family]) || (txOrder[a.transaction] - txOrder[b.transaction]) || a.title.localeCompare(b.title));

  const provisions = {};
  for (const agency of Object.values(AGENCIES)) {
    provisions[agency] = records.filter((r) => r.agency === agency && r.record_type !== "price_adjustment_grid_cell");
  }
  return { segments, provisions, gridCellCount: grid.length };
}

// ---------- Changes ----------

export const CHANGE_TYPES = {
  revised: "Revised",
  added: "New section",
  restructured: "Restructured",
  split: "Split into other sections",
  merged: "Merged into another section",
  removed: "Removed",
  renumbered: "Renumbered",
  retitled: "Retitled",
  took_effect: "Took effect",
  upcoming: "Upcoming (future-effective)",
  upcoming_revised: "Upcoming version revised",
  editorial: "Editorial (no new revision date)",
  layout_only: "Layout only",
  date_only: "Date only",
};
export const SUBSTANTIVE_TYPES = new Set(["revised", "added", "restructured", "split", "merged", "removed",
  "renumbered", "retitled", "took_effect", "upcoming", "upcoming_revised"]);

export function changeDate(c) {
  return c.nrev || c.eff || c.orev || "";
}

export function filterChanges(records, { types = null, month = "", text = "", announcement = "", period = "" } = {}) {
  const terms = tokenize(text);
  return records.filter((c) => (!types || types.has(c.type))
    && (!period || c.period === period)
    && (!month || changeDate(c).startsWith(month))
    && (!announcement || c.ann?.id === announcement || c.cited?.includes(announcement))
    && (!terms.length || terms.every((t) => `${c.sid} ${c.title} ${c.h} ${(c.add || []).join(" ")}`.toLowerCase().includes(t))))
    .sort((a, b) => changeDate(b).localeCompare(changeDate(a)) || a.sid.localeCompare(b.sid));
}

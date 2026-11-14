import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { deterministicChangeExplanation, explainChange, summarizeQuestion } from "../lib/ai.mjs";
import { loadSiteData } from "../lib/data.mjs";
import {
  SUBSTANTIVE_TYPES, crosswalkFor, deterministicSummary, filterChanges, prepareSections, pricingGridCatalog,
  pricingScenario, queryForAgency, repairText, retrieveEvidence, searchSections,
} from "../public/core.mjs";

const section = (id, t, x, extra = {}) => ({ id, rid: `${id}::current`, t, st: "current", rev: "2026-01-01", eff: null, h: "", p: 1, src: null, off: null, x, ...extra });

test("repairs common PDF extraction mojibake", () => {
  assert.equal(repairText("Seller/Servicerâ€™s"), "Seller/Servicer’s");
});

test("search ranks a title match above a body-only match", () => {
  const rows = prepareSections([
    section("B1", "Cash-out refinance", "General requirements"),
    section("B2", "Other topic", "A cash-out refinance appears in this body."),
  ], "fannie");
  assert.equal(searchSections(rows, "cash-out refinance")[0].id, "B1");
});

test("search by exact section number wins", () => {
  const rows = prepareSections([
    section("5501.1", "Funds required", "See also 5501.2 for details."),
    section("5501.2", "Other", "Unrelated"),
  ], "freddie");
  assert.equal(searchSections(rows, "5501.2")[0].id, "5501.2");
});

test("future-effective versions are excluded unless requested", () => {
  const rows = prepareSections([
    section("4301.5", "Cash-out refinance Mortgages", "current text"),
    section("4301.5", "Cash-out refinance Mortgages", "future text", { rid: "4301.5::future-2027-01-25", st: "future", eff: "2027-01-25" }),
  ], "freddie");
  assert.equal(searchSections(rows, "cash-out refinance").length, 1);
  assert.equal(searchSections(rows, "cash-out refinance", { includeFuture: true }).length, 2);
});

test("maps limited cash-out to each agency's term without treating it as cash-out", () => {
  assert.equal(queryForAgency("limited cash-out refinance requirements", "Fannie Mae"), "limited cash-out refinance requirements");
  assert.equal(queryForAgency("limited cash-out refinance requirements", "Freddie Mac"), "no cash-out refinance requirements");
  assert.equal(queryForAgency("cash-out refinance seasoning", "Freddie Mac"), "cash-out refinance seasoning");
});

test("limited cash-out questions retrieve the agency-named sections", () => {
  const freddie = prepareSections([
    section("4301.4", "“No cash-out” refinance Mortgages", "no cash-out refinance rules"),
    section("4301.5", "Cash-out refinance Mortgages", "cash-out refinance rules refinance refinance"),
  ], "freddie");
  const { evidence } = retrieveEvidence(freddie, "How do limited cash-out refinance requirements differ?", "Freddie Mac");
  assert.equal(evidence[0].id, "4301.4");
});

test("pricing scenario sums base and selected attributes without caps", () => {
  const cell = (agency, family, extra) => ({ record_type: "price_adjustment_grid_cell", agency, pricing_family: family, transaction_category: "purchase", ltv_min_exclusive: 75, ltv_max_inclusive: 80, ...extra });
  const records = [
    cell("Fannie Mae", "base", { score_band: "740_759", score_min_inclusive: 740, score_max_exclusive: 760, fee_percent: 0.875 }),
    cell("Fannie Mae", "attribute", { score_band: "all_scores", attribute_key: "condominium", attribute_label: "Condo", fee_percent: 0.75 }),
    cell("Freddie Mac", "base", { score_band: "740_759", score_min_inclusive: 740, score_max_exclusive: 760, fee_percent: 0.875 }),
    cell("Freddie Mac", "attribute", { score_band: "all_scores", attribute_key: "condominium", attribute_label: "Condominium Unit", fee_percent: 0.75 }),
  ];
  const result = pricingScenario(records, { transaction: "purchase", score: 745, ltv: 78, attributes: ["condominium"] });
  assert.equal(result.agencies["Fannie Mae"].uncappedFeePercent, 1.625);
  assert.equal(result.deltaFannieMinusFreddie, 0);
  assert.equal(pricingGridCatalog(records).segments.length, 2);
});

test("deterministic summary flags numeric differences and the non-equivalence caveat", () => {
  const result = deterministicSummary({ title: "Income", text: "The lender must document 12 months of income." },
    { title: "Income", text: "The Seller must document 24 months of income." }, "income");
  assert.ok(result.differences.some((d) => d.dimension === "Thresholds and time periods"));
  assert.equal(result.mode, "deterministic");
});

test("AI functions fall back to evidence-only output when no key is configured", async () => {
  const ai = { enabled: false };
  const ev = [{ agency: "Fannie Mae", id: "B1", title: "Income", page: 1, link: null, text: "must document 12 months" }];
  const summary = await summarizeQuestion({ question: "income", fannieEvidence: ev, freddieEvidence: [{ ...ev[0], agency: "Freddie Mac" }], ai });
  assert.equal(summary.mode, "deterministic");
  const change = { sid: "B3-3.1-04", title: "Verbal VOE", type: "revised", sa: 3, sr: 2, sim: 0.8, add: ["x"], rem: ["y"] };
  assert.deepEqual(await explainChange({ change, ai }), deterministicChangeExplanation(change));
});

test("change filters default to substantive types", () => {
  const records = [{ type: "revised", sid: "A", title: "x", h: "", nrev: "2026-10-07" }, { type: "layout_only", sid: "B", title: "y", h: "", nrev: "2026-10-07" }];
  assert.deepEqual(filterChanges(records, { types: SUBSTANTIVE_TYPES }).map((r) => r.sid), ["A"]);
});

const dataDir = fileURLToPath(new URL("../public/data", import.meta.url));
test("built site data is internally consistent", { skip: !existsSync(`${dataDir}/meta.json`) && "run pipeline/build_site_data.py first" }, () => {
  const data = loadSiteData(dataDir);
  assert.ok(data.fannie.length > 400 && data.freddie.length > 500);
  const ids = new Set(data.freddie.map((s) => s.id));
  const { candidates } = crosswalkFor({ byFannie: {}, curated: [] }, "none");
  assert.equal(candidates.length, 0);
  for (const c of data.changes.freddie.records) if (c.nsid) assert.ok(ids.has(c.nsid), `${c.nsid} missing`);
  const { evidence } = retrieveEvidence(data.freddie, "limited cash-out refinance", "Freddie Mac");
  assert.equal(evidence[0].id, "4301.4");
});

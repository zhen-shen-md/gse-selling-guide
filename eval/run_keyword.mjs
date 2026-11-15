// Runs the production keyword retriever (web/public/core.mjs, the same code the website uses) over the
// evaluation questions and writes ranked section IDs per agency for eval/run_eval.py.
// Usage: node eval/run_keyword.mjs   (requires web/public/data, built by pipeline/build_site_data.py)
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { loadSiteData } from "../web/lib/data.mjs";
import { AGENCIES, queryForAgency, searchSections } from "../web/public/core.mjs";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const data = loadSiteData(here("../web/public/data"));
const questions = readFileSync(here("./questions.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));

const out = {};
for (const q of questions) {
  out[q.id] = {};
  for (const [key, name] of Object.entries(AGENCIES)) {
    const mapped = queryForAgency(q.question, name);
    const ids = [];
    for (const r of searchSections(data[key], mapped, { limit: 50 })) if (!ids.includes(r.id)) ids.push(r.id);
    out[q.id][key] = { query: mapped, ranking: ids };
  }
}
mkdirSync(here("./.cache"), { recursive: true });
writeFileSync(here("./.cache/keyword_rankings.json"), JSON.stringify(out));
console.log(`keyword rankings written for ${questions.length} questions`);

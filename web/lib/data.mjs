// Node-side loader: reads the same JSON files the static site fetches, so the server and the
// browser always search identical data.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { prepareSections } from "../public/core.mjs";

export function loadSiteData(dataDir) {
  const read = (name) => {
    const path = join(dataDir, name);
    if (!existsSync(path)) {
      throw new Error(`${path} is missing. Run: python pipeline/build_site_data.py`);
    }
    return JSON.parse(readFileSync(path, "utf8"));
  };
  const fannie = prepareSections(read("sections-fannie.json"), "fannie");
  const freddie = prepareSections(read("sections-freddie.json"), "freddie");
  const changes = read("changes.json");
  const changeById = new Map();
  for (const agency of Object.values(changes)) for (const c of agency.records) changeById.set(c.cid, c);
  return {
    meta: read("meta.json"),
    fannie,
    freddie,
    sectionByRid: new Map([...fannie, ...freddie].map((s) => [`${s.agencyKey}:${s.rid}`, s])),
    changes,
    changeById,
  };
}

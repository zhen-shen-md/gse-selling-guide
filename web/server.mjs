// Optional local/hosted server. It serves the same static site as GitHub Pages and adds AI endpoints
// when OPENAI_API_KEY is set. Without a key, the AI endpoints return evidence-only results.
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { aiConfig, explainChange, summarizeQuestion } from "./lib/ai.mjs";
import { AGENCIES, retrieveEvidence } from "./public/core.mjs";
import { loadSiteData } from "./lib/data.mjs";

const WEB_DIR = fileURLToPath(new URL(".", import.meta.url));
const PUBLIC_DIR = join(WEB_DIR, "public");
const PORT = Number(process.env.PORT || 4173);
const HOST = process.env.HOST || "127.0.0.1";
const ai = aiConfig();
const data = loadSiteData(join(PUBLIC_DIR, "data"));

const MIME = { ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml" };
const SECURITY = {
  "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'",
  "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
};

function json(res, status, payload) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...SECURITY });
  res.end(JSON.stringify(payload));
}

async function bodyJson(req) {
  let body = "";
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 100_000) throw new Error("Request body is too large.");
  }
  return body ? JSON.parse(body) : {};
}

function serveStatic(res, pathname) {
  const requested = pathname === "/" ? "index.html" : decodeURIComponent(pathname.slice(1));
  const file = normalize(join(PUBLIC_DIR, requested));
  if (!file.startsWith(PUBLIC_DIR + sep) || !existsSync(file) || statSync(file).isDirectory()) return false;
  res.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream", "Cache-Control": "no-cache", ...SECURITY });
  createReadStream(file).pipe(res);
  return true;
}

const evidenceText = (row) => ({ ...row, text: data.sectionByRid.get(`${row.agencyKey}:${row.rid}`)?.x ?? "" });

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || `${HOST}:${PORT}`}`);

    if (req.method === "GET" && url.pathname === "/api/status") {
      return json(res, 200, { server: true, ai: { enabled: ai.enabled, model: ai.enabled ? ai.model : null } });
    }

    if (req.method === "POST" && url.pathname === "/api/ask") {
      const question = String((await bodyJson(req)).question || "").trim().slice(0, 500);
      if (question.length < 2) return json(res, 400, { error: "Ask a question with at least two characters." });
      // Evidence is always retrieved on the server; client-supplied evidence is never trusted.
      const f = retrieveEvidence(data.fannie, question, AGENCIES.fannie);
      const r = retrieveEvidence(data.freddie, question, AGENCIES.freddie);
      if (!f.evidence.length || !r.evidence.length) {
        return json(res, 404, { error: "Could not find evidence in both guides. Try a more specific mortgage-policy phrase." });
      }
      const summary = await summarizeQuestion({
        question, ai, fannieEvidence: f.evidence.map(evidenceText), freddieEvidence: r.evidence.map(evidenceText),
      });
      return json(res, 200, { question, retrieval: { fannie: f.retrievalQuery, freddie: r.retrievalQuery }, summary });
    }

    if (req.method === "POST" && url.pathname === "/api/explain-change") {
      const change = data.changeById.get(String((await bodyJson(req)).changeId || ""));
      if (!change) return json(res, 404, { error: "Unknown change record." });
      return json(res, 200, { changeId: change.cid, explanation: await explainChange({ change, ai }) });
    }

    if (req.method === "GET" && serveStatic(res, url.pathname)) return;
    return json(res, 404, { error: "Not found." });
  } catch (error) {
    console.error(error);
    return json(res, 500, { error: error instanceof Error ? error.message : "Unexpected server error." });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`GSE Selling Guide KB running at http://${HOST}:${PORT}`);
  console.log(`AI: ${ai.enabled ? `enabled (${ai.model})` : "off — evidence-only mode (set OPENAI_API_KEY to enable)"}`);
});

export { server };

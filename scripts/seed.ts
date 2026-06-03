#!/usr/bin/env node
/**
 * Seed a Tollgate store with sample requests + findings so the dashboard can be
 * viewed without sending real traffic. For demos and manual QA only.
 *
 * Usage:
 *   npx tsx scripts/seed.ts [dbPath]
 * Then point the proxy's storage at that path (or copy it to ~/.tollgate) and
 * open http://127.0.0.1:8787/_tollgate
 */
import { randomUUID } from "node:crypto";
import { Repo, type RequestRecord } from "../src/store/repo.js";

const dbPath = process.argv[2] ?? "/tmp/tollgate-demo.db";
const repo = new Repo(dbPath);

const DAY = 24 * 60 * 60 * 1000;
const now = Date.now();

type Sample = Partial<RequestRecord> & { findingsCount?: number };

const models = [
  { provider: "anthropic", model: "claude-haiku-4-5", inUnit: 1.0, outUnit: 5.0 },
  { provider: "anthropic", model: "claude-sonnet-4-5", inUnit: 3.0, outUnit: 15.0 },
  { provider: "openai", model: "gpt-4o", inUnit: 2.5, outUnit: 10.0 },
];
const types = ["short-prompt", "long-context", "multimodal"];

let seeded = 0;
for (let d = 4; d >= 0; d--) {
  const perDay = 3 + ((d * 2) % 4);
  for (let i = 0; i < perDay; i++) {
    const m = models[(d + i) % models.length];
    const type = types[(d + i) % types.length];
    const inTok = type === "long-context" ? 12000 + i * 500 : type === "multimodal" ? 4000 : 300 + i * 40;
    const outTok = 200 + i * 30;
    const id = randomUUID();
    const sample: Sample = {
      id,
      ts: now - d * DAY - i * 1000 * 60,
      provider: m.provider,
      model: m.model,
      routeLabel: m.provider,
      inputTokensEst: Math.round(inTok * 0.95),
      inputTokensActual: inTok,
      outputTokensActual: outTok,
      estInputCost: (inTok / 1e6) * m.inUnit,
      estOutputCost: (outTok / 1e6) * m.outUnit,
      upstreamMs: 250 + i * 17,
      contentHash: randomUUID().replace(/-/g, ""),
      rawLogged: false,
      requestType: type,
    };
    repo.insertRequest(sample as RequestRecord);
    if (type === "long-context") {
      repo.insertFindings(id, [
        { rule: "oversized-paste", severity: "high", tokensWastedEst: Math.round(inTok * 0.6), message: `Large pasted block (~${inTok} tokens).`, location: { messageIndex: 0 } },
      ]);
    }
    if (type === "multimodal") {
      repo.insertFindings(id, [
        { rule: "multimodal-surprise", severity: "info", tokensWastedEst: 0, message: "PDF input ≈ 6,000 tokens.", location: { messageIndex: 0 } },
      ]);
    }
    seeded++;
  }
}

const s = repo.summary();
repo.close();
console.log(`Seeded ${seeded} requests into ${dbPath}`);
console.log(`Totals: ${s.requests} requests, ${s.inputTokens.toLocaleString()} input tokens, $${s.cost.toFixed(4)} est spend`);
console.log(`View: set storage.path = "${dbPath}" and open http://127.0.0.1:8787/_tollgate`);

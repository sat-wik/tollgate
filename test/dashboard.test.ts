import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { loadConfig } from "../src/config/index.js";
import { buildServer, type TollgateServer } from "../src/server/index.js";
import { Repo, type RequestRecord } from "../src/store/repo.js";

let tmp: string;
let dbPath: string;
let server: TollgateServer;
const T0 = Date.parse("2026-06-01T12:00:00Z");

function rec(p: Partial<RequestRecord> & { id: string }): RequestRecord {
  return {
    ts: T0,
    provider: "anthropic",
    model: "claude-haiku-4-5",
    routeLabel: "anthropic",
    inputTokensEst: 100,
    inputTokensActual: 120,
    outputTokensActual: 50,
    estInputCost: 0.0001,
    estOutputCost: 0.0002,
    upstreamMs: 300,
    contentHash: "abc123",
    rawLogged: false,
    requestType: "short-prompt",
    ...p,
  };
}

beforeAll(async () => {
  tmp = mkdtempSync(join(tmpdir(), "tollgate-dash-"));
  dbPath = join(tmp, "dash.db");

  // Seed a DB, then close it so the server opens the same file.
  const seed = new Repo(dbPath);
  seed.insertRequest(rec({ id: "r1", ts: T0, model: "claude-haiku-4-5", requestType: "short-prompt" }));
  seed.insertRequest(
    rec({
      id: "r2",
      ts: T0 + 1000,
      model: "claude-opus-4-1",
      requestType: "long-context",
      inputTokensActual: 9000,
      estInputCost: 0.05,
      estOutputCost: 0.01, // input is the top driver here
    }),
  );
  seed.insertRequest(
    rec({ id: "r3", ts: T0 + 2000, model: "claude-haiku-4-5", estInputCost: 0.0001, estOutputCost: 0.5 }),
  );
  seed.insertFindings("r3", [
    { rule: "oversized-paste", severity: "high", tokensWastedEst: 5000, message: "big block", location: { messageIndex: 0 } },
  ]);
  seed.close();

  const config = loadConfig({ overrides: { storagePath: dbPath, port: 0 } });
  server = buildServer(config);
  await server.app.ready();
});

afterAll(async () => {
  await server.app.close();
  rmSync(tmp, { recursive: true, force: true });
});

async function get(url: string) {
  return server.app.inject({ method: "GET", url });
}

describe("M4 dashboard API", () => {
  it("summary aggregates totals and per-model breakdown", async () => {
    const res = await get("/_tollgate/api/summary");
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.summary.requests).toBe(3);
    // input tokens: 120 + 9000 + 120 = 9240
    expect(body.summary.inputTokens).toBe(9240);
    expect(body.summary.outputTokens).toBe(150);
    expect(body.summary.cost).toBeCloseTo(0.0003 + 0.06 + 0.5001, 6);

    const models = Object.fromEntries(body.byModel.map((b: any) => [b.key, b.requests]));
    expect(models["claude-haiku-4-5"]).toBe(2);
    expect(models["claude-opus-4-1"]).toBe(1);
  });

  it("breaks down by request type", async () => {
    const body = (await get("/_tollgate/api/summary")).json();
    const types = Object.fromEntries(body.byType.map((b: any) => [b.key, b.requests]));
    expect(types["short-prompt"]).toBe(2);
    expect(types["long-context"]).toBe(1);
  });

  it("lists recent requests newest-first with their findings", async () => {
    const body = (await get("/_tollgate/api/requests")).json();
    expect(body.map((r: any) => r.id)).toEqual(["r3", "r2", "r1"]);
    const r3 = body[0];
    expect(r3.findings).toHaveLength(1);
    expect(r3.findings[0].rule).toBe("oversized-paste");
  });

  it("returns an accurate receipt with the top cost driver", async () => {
    const r2 = (await get("/_tollgate/receipt/r2")).json();
    expect(r2.inputCost).toBeCloseTo(0.05, 6);
    expect(r2.outputCost).toBeCloseTo(0.01, 6);
    expect(r2.totalCost).toBeCloseTo(0.06, 6);
    expect(r2.topCostDriver).toBe("input");

    const r3 = (await get("/_tollgate/receipt/r3")).json();
    expect(r3.topCostDriver).toBe("output");
    expect(r3.findings).toHaveLength(1);
  });

  it("404s for an unknown receipt id", async () => {
    expect((await get("/_tollgate/receipt/nope")).statusCode).toBe(404);
  });

  it("serves the dashboard HTML page", async () => {
    const res = await get("/_tollgate");
    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toContain("text/html");
    expect(res.body).toContain("Tollgate");
    // Fully offline: no external resource references.
    expect(res.body).not.toMatch(/https?:\/\/[^"']*\.(js|css)/);
  });
});

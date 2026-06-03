import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadConfig, type Config } from "../src/config/index.js";
import { buildServer, type TollgateServer } from "../src/server/index.js";
import { DEFAULT_LINT_CONFIG } from "../src/lint/rules/types.js";
import { DEFAULT_CACHE_CONFIG } from "../src/cache/detector.js";
import { startMockUpstream, type MockUpstream } from "./upstream-mock.js";

let mock: MockUpstream;
let server: TollgateServer;

function testConfig(): Config {
  // Point both routes at the mock upstream and use an in-memory store.
  return {
    port: 0,
    storagePath: ":memory:",
    routes: [
      {
        provider: "anthropic",
        path: "/v1/messages",
        upstream: mock.url,
        label: "anthropic",
        rawLog: false,
        injectUsage: false,
      },
      {
        provider: "openai",
        path: "/v1/chat/completions",
        upstream: mock.url,
        label: "openai",
        rawLog: false,
        injectUsage: true,
      },
    ],
    budget: { thresholds: [0.8, 1.0], block: false },
    // Give the mock models a price so cost columns are exercised.
    pricingOverrides: {
      "anthropic/claude-test": { inputPerMTok: 1.0, outputPerMTok: 5.0 },
      "openai/gpt-test": { inputPerMTok: 2.5, outputPerMTok: 10.0 },
    },
    lint: DEFAULT_LINT_CONFIG,
    // Lower the prefix threshold so the caching path is exercisable with a
    // modest prompt (default 1024 tokens would need a very large fixture).
    cache: { ...DEFAULT_CACHE_CONFIG, minPrefixTokens: 50 },
  };
}

async function inject(path: string, body: unknown) {
  return server.app.inject({
    method: "POST",
    url: path,
    headers: { "content-type": "application/json", authorization: "Bearer secret-key" },
    payload: Buffer.from(JSON.stringify(body)),
  });
}

beforeEach(async () => {
  mock = await startMockUpstream();
  server = buildServer(testConfig());
  await server.app.ready();
});

afterEach(async () => {
  await server.app.close();
  await mock.close();
});

describe("M1 transparent proxy", () => {
  it("forwards the Anthropic request body byte-for-byte", async () => {
    const body = { model: "claude-test", max_tokens: 100, messages: [{ role: "user", content: "hi there" }] };
    const expected = Buffer.from(JSON.stringify(body));

    const res = await inject("/v1/messages", body);
    expect(res.statusCode).toBe(200);

    expect(mock.requests).toHaveLength(1);
    expect(mock.requests[0].body.equals(expected)).toBe(true);
  });

  it("passes provider auth headers through to the upstream", async () => {
    await inject("/v1/messages", { model: "claude-test", messages: [] });
    expect(mock.requests[0].headers.authorization).toBe("Bearer secret-key");
  });

  it("relays the upstream JSON response body unchanged", async () => {
    const res = await inject("/v1/messages", { model: "claude-test", messages: [{ role: "user", content: "hi" }] });
    const json = JSON.parse(res.body);
    expect(json.content[0].text).toBe("hello from anthropic");
    expect(res.headers["content-type"]).toContain("application/json");
  });

  it("captures usage and metadata to the store (Anthropic JSON)", async () => {
    await inject("/v1/messages", { model: "claude-test", messages: [{ role: "user", content: "hi" }] });
    const rows = server.repo.recentRequests();
    expect(rows).toHaveLength(1);
    expect(rows[0].provider).toBe("anthropic");
    expect(rows[0].model).toBe("claude-test");
    expect(rows[0].routeLabel).toBe("anthropic");
    expect(rows[0].inputTokensActual).toBe(42);
    expect(rows[0].outputTokensActual).toBe(7);
    expect(rows[0].contentHash).toMatch(/^[0-9a-f]{64}$/);
    expect(rows[0].rawLogged).toBe(false);
    expect(rows[0].upstreamMs).toBeGreaterThanOrEqual(0);
  });

  it("persists the pre-flight token estimate and computed cost (M2)", async () => {
    const res = await inject("/v1/messages", {
      model: "claude-test",
      messages: [{ role: "user", content: "Estimate my tokens please, this is a longer message." }],
    });
    // Pre-flight estimate is surfaced as response headers.
    expect(Number(res.headers["x-tollgate-input-tokens-est"])).toBeGreaterThan(0);
    expect(res.headers["x-tollgate-input-accuracy"]).toBe("approx");

    const row = server.repo.recentRequests()[0];
    expect(row.inputTokensEst).toBeGreaterThan(0);
    // Cost computed from actual usage (in=42, out=7) at 1.0/5.0 per MTok.
    expect(row.estInputCost).toBeCloseTo((42 / 1_000_000) * 1.0, 9);
    expect(row.estOutputCost).toBeCloseTo((7 / 1_000_000) * 5.0, 9);
  });

  it("runs the lint engine and persists findings for a wasteful prompt (M3)", async () => {
    const big = Array.from({ length: 6000 }, (_, i) => `w${i % 97}`).join(" ");
    const res = await inject("/v1/messages", {
      model: "claude-test",
      messages: [{ role: "user", content: big }],
    });
    expect(Number(res.headers["x-tollgate-lint-findings"])).toBeGreaterThanOrEqual(1);
    expect(Number(res.headers["x-tollgate-tokens-wasted-est"])).toBeGreaterThan(0);

    const row = server.repo.recentRequests()[0];
    const findings = server.repo.getFindings(row.id);
    expect(findings.some((f) => f.rule === "oversized-paste")).toBe(true);
  });

  it("detects a caching opportunity on a repeated prefix (M3)", async () => {
    const system = Array.from({ length: 120 }, (_, i) => `ctx${i % 40}`).join(" ");
    const body = (q: string) => ({ model: "claude-test", system, messages: [{ role: "user", content: q }] });

    await inject("/v1/messages", body("first question")); // seeds the window
    await inject("/v1/messages", body("second question")); // shares the prefix

    const rows = server.repo.recentRequests();
    const latest = server.repo.getFindings(rows[0].id);
    const cacheFinding = latest.find((f) => f.rule === "cache-opportunity");
    expect(cacheFinding).toBeDefined();
    expect(cacheFinding!.tokensWastedEst).toBeGreaterThanOrEqual(50);
  });

  it("reports estimated cost as unknown for an unpriced model", async () => {
    const res = await inject("/v1/messages", {
      model: "claude-unpriced-model",
      messages: [{ role: "user", content: "hi" }],
    });
    expect(res.headers["x-tollgate-est-input-cost-usd"]).toBe("unknown");
    const row = server.repo.recentRequests()[0];
    expect(row.estInputCost).toBeNull();
    expect(row.estOutputCost).toBeNull();
  });

  it("captures usage from a streamed Anthropic response", async () => {
    const res = await inject("/v1/messages", {
      model: "claude-test",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toContain("text/event-stream");
    expect(res.body).toContain("message_start");

    const rows = server.repo.recentRequests();
    expect(rows[0].inputTokensActual).toBe(42);
    expect(rows[0].outputTokensActual).toBe(7);
  });

  it("captures usage from a streamed OpenAI response", async () => {
    const res = await inject("/v1/chat/completions", {
      model: "gpt-test",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    expect(res.statusCode).toBe(200);

    const rows = server.repo.recentRequests();
    expect(rows[0].provider).toBe("openai");
    expect(rows[0].inputTokensActual).toBe(42);
    expect(rows[0].outputTokensActual).toBe(7);
  });

  it("injects stream_options.include_usage for OpenAI streaming (the one sanctioned mutation)", async () => {
    await inject("/v1/chat/completions", {
      model: "gpt-test",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    const forwarded = JSON.parse(mock.requests[0].body.toString("utf8"));
    expect(forwarded.stream_options).toEqual({ include_usage: true });
  });

  it("does NOT mutate OpenAI non-streaming requests", async () => {
    const body = { model: "gpt-test", messages: [{ role: "user", content: "hi" }] };
    await inject("/v1/chat/completions", body);
    expect(mock.requests[0].body.equals(Buffer.from(JSON.stringify(body)))).toBe(true);
  });

  it("does NOT mutate Anthropic requests even when streaming", async () => {
    const body = { model: "claude-test", stream: true, messages: [{ role: "user", content: "hi" }] };
    await inject("/v1/messages", body);
    expect(mock.requests[0].body.equals(Buffer.from(JSON.stringify(body)))).toBe(true);
  });
});

describe("config loader", () => {
  it("loads bundled defaults with both provider routes", () => {
    const cfg = loadConfig({ userConfigPath: "/nonexistent/config.toml" });
    const labels = cfg.routes.map((r) => r.label).sort();
    expect(labels).toEqual(["anthropic", "openai"]);
    const openai = cfg.routes.find((r) => r.provider === "openai")!;
    expect(openai.injectUsage).toBe(true);
    expect(openai.rawLog).toBe(false);
  });
});

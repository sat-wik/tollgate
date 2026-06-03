#!/usr/bin/env node
/**
 * End-to-end validator for the Tollgate proxy against a REAL provider upstream.
 *
 * Unlike the Vitest suite (which uses a local mock, per CLAUDE.md §8), this script
 * sends a small real request through the proxy to api.anthropic.com or
 * api.openai.com and verifies the full path: routing, auth passthrough, live
 * streaming, and per-request capture to SQLite.
 *
 * Usage:
 *   ANTHROPIC_API_KEY=sk-... npx tsx scripts/e2e.ts anthropic
 *   OPENAI_API_KEY=sk-...    npx tsx scripts/e2e.ts openai
 *
 * Optional: TOLLGATE_E2E_MODEL=<model-id> to override the default model.
 *
 * This makes a real, billable API call (a few tokens). Nothing is persisted
 * outside a throwaway temp database.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { loadConfig } from "../src/config/index.js";
import { buildServer } from "../src/server/index.js";

type Provider = "anthropic" | "openai";

const provider = (process.argv[2] as Provider) ?? "anthropic";
if (provider !== "anthropic" && provider !== "openai") {
  console.error(`Unknown provider "${provider}". Use "anthropic" or "openai".`);
  process.exit(2);
}

const key = provider === "anthropic" ? process.env.ANTHROPIC_API_KEY : process.env.OPENAI_API_KEY;
if (!key) {
  const envName = provider === "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY";
  console.error(`Missing ${envName}. Set it and re-run.`);
  process.exit(2);
}

const model =
  process.env.TOLLGATE_E2E_MODEL ??
  (provider === "anthropic" ? "claude-haiku-4-5" : "gpt-4o-mini");

// A deliberately wasteful prompt (duplicated oversized block) to exercise the
// M3 lint rules end-to-end. Enable with TOLLGATE_E2E_WASTEFUL=1.
function wastefulPrompt(): string {
  const block = Array.from({ length: 5000 }, (_, i) => `word${i % 200}`).join(" ");
  return `Here is a file:\n${block}\n\nAnd here is the same file again:\n${block}\n\nSummarize it.`;
}

function buildRequest(stream: boolean): { path: string; headers: Record<string, string>; body: unknown } {
  const prompt =
    process.env.TOLLGATE_E2E_WASTEFUL === "1"
      ? wastefulPrompt()
      : "Reply with exactly one word: pong";
  if (provider === "anthropic") {
    return {
      path: "/v1/messages",
      headers: { "content-type": "application/json", "x-api-key": key!, "anthropic-version": "2023-06-01" },
      body: { model, max_tokens: 16, stream, messages: [{ role: "user", content: prompt }] },
    };
  }
  return {
    path: "/v1/chat/completions",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: { model, max_tokens: 16, stream, messages: [{ role: "user", content: prompt }] },
  };
}

async function runOnce(base: string, stream: boolean) {
  const req = buildRequest(stream);
  const t0 = Date.now();
  const res = await fetch(base + req.path, {
    method: "POST",
    headers: req.headers,
    body: JSON.stringify(req.body),
  });

  let firstByteMs: number | null = null;
  let bytes = 0;
  const collected: string[] = [];
  if (res.body) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (firstByteMs === null) firstByteMs = Date.now() - t0;
      bytes += value.byteLength;
      collected.push(dec.decode(value, { stream: true }));
    }
  }
  return {
    status: res.status,
    contentType: res.headers.get("content-type") ?? "",
    firstByteMs,
    totalMs: Date.now() - t0,
    bytes,
    text: collected.join(""),
    estTokens: res.headers.get("x-tollgate-input-tokens-est"),
    estAccuracy: res.headers.get("x-tollgate-input-accuracy"),
    estCost: res.headers.get("x-tollgate-est-input-cost-usd"),
  };
}

function summarizeText(contentType: string, text: string): string {
  // Pull a short human-readable snippet out of either an SSE or JSON response.
  if (contentType.includes("text/event-stream")) {
    const pieces: string[] = [];
    for (const line of text.split(/\r?\n/)) {
      const t = line.trim();
      if (!t.startsWith("data:")) continue;
      const payload = t.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      try {
        const o = JSON.parse(payload);
        const piece =
          o?.delta?.text ?? o?.choices?.[0]?.delta?.content ?? "";
        if (piece) pieces.push(piece);
      } catch {
        /* ignore */
      }
    }
    return pieces.join("");
  }
  try {
    const o = JSON.parse(text);
    return o?.content?.[0]?.text ?? o?.choices?.[0]?.message?.content ?? text.slice(0, 200);
  } catch {
    return text.slice(0, 200);
  }
}

async function main(): Promise<void> {
  const tmp = mkdtempSync(join(tmpdir(), "tollgate-e2e-"));
  const dbPath = join(tmp, "e2e.db");
  const config = loadConfig({ overrides: { storagePath: dbPath, port: 0 } });
  const { app, repo } = buildServer(config);
  await app.listen({ port: 0, host: "127.0.0.1" });
  const port = (app.server.address() as AddressInfo).port;
  const base = `http://127.0.0.1:${port}`;

  console.log(`\nTollgate E2E — provider=${provider} model=${model}`);
  console.log(`Proxy listening on ${base}, forwarding to the real upstream.\n`);

  let failures = 0;
  for (const stream of [false, true]) {
    const label = stream ? "streaming" : "non-streaming";
    try {
      const r = await runOnce(base, stream);
      const ok = r.status >= 200 && r.status < 300;
      if (!ok) failures++;
      console.log(`── ${label} ──────────────────────────────`);
      console.log(`  HTTP status        : ${r.status} ${ok ? "✓" : "✗"}`);
      console.log(`  content-type       : ${r.contentType}`);
      console.log(`  time to first byte : ${r.firstByteMs ?? "n/a"} ms`);
      console.log(`  total time         : ${r.totalMs} ms`);
      console.log(`  bytes relayed      : ${r.bytes}`);
      console.log(`  pre-flight estimate: ${r.estTokens} input tokens (${r.estAccuracy}), est input cost $${r.estCost}`);
      if (ok) {
        console.log(`  model reply        : ${JSON.stringify(summarizeText(r.contentType, r.text))}`);
      } else {
        console.log(`  upstream error body: ${r.text.slice(0, 300)}`);
      }
      console.log("");
    } catch (err) {
      failures++;
      console.log(`── ${label} ── ERROR: ${String(err)}\n`);
    }
  }

  // Give finalize() a beat to write the records, then read them back.
  await new Promise((r) => setTimeout(r, 100));
  const rows = repo.recentRequests();
  console.log(`── captured to SQLite (${rows.length} rows) ───────────`);
  let withinBand = 0;
  let comparable = 0;
  for (const row of rows) {
    const est = row.inputTokensEst;
    const act = row.inputTokensActual;
    let deltaStr = "";
    if (est != null && act != null && act > 0) {
      comparable++;
      const delta = (est - act) / act;
      if (Math.abs(delta) <= 0.1) withinBand++;
      deltaStr = `  est=${est} (Δ ${(delta * 100).toFixed(1)}%)`;
    }
    console.log(
      `  ${row.provider}/${row.model}  in=${act ?? "?"} out=${row.outputTokensActual ?? "?"}${deltaStr} ` +
        `cost=$${(((row.estInputCost ?? 0) + (row.estOutputCost ?? 0)) || 0).toFixed(6)} ` +
        `upstream=${row.upstreamMs}ms  raw_logged=${row.rawLogged}`,
    );
    for (const f of repo.getFindings(row.id)) {
      console.log(`      ⚠ [${f.rule}/${f.severity}] ~${f.tokensWastedEst} tok — ${f.message}`);
    }
  }
  console.log("");

  const captured = rows.length;
  const withUsage = rows.filter((r) => r.inputTokensActual != null && r.outputTokensActual != null).length;
  console.log("── verdict ───────────────────────────────");
  console.log(`  requests captured            : ${captured}/2 ${captured === 2 ? "✓" : "✗"}`);
  console.log(`  usage captured (in+out)      : ${withUsage}/2 ${withUsage === 2 ? "✓" : "✗"}`);
  console.log(`  successful responses         : ${2 - failures}/2 ${failures === 0 ? "✓" : "✗"}`);
  // The ±10% band is documented for natural English; the synthetic wasteful
  // prompt uses pathological token soup and is not a fair input for that check.
  const wasteful = process.env.TOLLGATE_E2E_WASTEFUL === "1";
  const bandOk = wasteful || (comparable > 0 && withinBand === comparable);
  console.log(
    `  estimate within ±10%         : ${withinBand}/${comparable} ${
      wasteful ? "(n/a — synthetic prompt)" : comparable > 0 && withinBand === comparable ? "✓" : "✗"
    }`,
  );

  await app.close();
  rmSync(tmp, { recursive: true, force: true });
  process.exit(failures === 0 && captured === 2 && withUsage === 2 && bandOk ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

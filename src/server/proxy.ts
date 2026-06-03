import http from "node:http";
import https from "node:https";
import { randomUUID } from "node:crypto";
import type { FastifyReply, FastifyRequest } from "fastify";
import type { RouteConfig } from "../config/index.js";
import type { Repo } from "../store/repo.js";
import { parseRequest } from "../adapters/index.js";
import { contentHash } from "../util/hash.js";
import { extractUsage } from "./usage.js";
import { getTokenizer } from "../tokenizer/index.js";
import type { Pricing } from "../pricing/index.js";
import type { BudgetTracker } from "../budget/tracker.js";

export type ProxyDeps = {
  repo: Repo;
  pricing: Pricing;
  budget: BudgetTracker;
};

// Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). content-length is
// recomputed from the forwarded body; host is derived from the upstream URL.
const STRIP_REQUEST_HEADERS = new Set([
  "host",
  "content-length",
  "connection",
  "keep-alive",
  "proxy-connection",
  "transfer-encoding",
  "te",
  "trailer",
  "upgrade",
]);

function filterRequestHeaders(
  headers: NodeJS.Dict<string | string[]>,
): http.OutgoingHttpHeaders {
  const out: http.OutgoingHttpHeaders = {};
  for (const [k, v] of Object.entries(headers)) {
    if (v == null) continue;
    if (STRIP_REQUEST_HEADERS.has(k.toLowerCase())) continue;
    out[k] = v;
  }
  return out;
}

/**
 * Apply the single sanctioned request mutation (CLAUDE.md §7): for a streaming
 * OpenAI request lacking stream_options.include_usage, inject it so the final
 * chunk carries token usage. Returns the (possibly new) body to forward.
 */
function maybeInjectUsage(
  route: RouteConfig,
  parsed: Record<string, unknown>,
  rawBody: Buffer,
): { body: Buffer; mutated: boolean } {
  if (route.provider !== "openai" || !route.injectUsage) return { body: rawBody, mutated: false };
  if (parsed.stream !== true) return { body: rawBody, mutated: false };
  const opts = (parsed.stream_options ?? {}) as Record<string, unknown>;
  if (opts.include_usage === true) return { body: rawBody, mutated: false };
  const next = { ...parsed, stream_options: { ...opts, include_usage: true } };
  return { body: Buffer.from(JSON.stringify(next)), mutated: true };
}

export function createProxyHandler(route: RouteConfig, deps: ProxyDeps) {
  const { repo, pricing, budget } = deps;
  const upstream = new URL(route.upstream);
  const client = upstream.protocol === "https:" ? https : http;
  const tokenizer = getTokenizer(route.provider);
  const modelKeyFor = (model: string): string => `${route.provider}/${model}`;

  return async function handler(req: FastifyRequest, reply: FastifyReply): Promise<void> {
    const rawBody = req.body as Buffer;

    let parsed: Record<string, unknown> = {};
    try {
      parsed = rawBody.length ? (JSON.parse(rawBody.toString("utf8")) as Record<string, unknown>) : {};
    } catch {
      // Not JSON we understand; forward verbatim and record what little we can.
    }

    const normalized = parseRequest(route.provider, parsed, rawBody);
    const { body: forwardBody } = maybeInjectUsage(route, parsed, rawBody);

    // Pre-flight estimate (PRD §5.2): count input tokens locally before forwarding.
    const estimate = tokenizer.countMessages(normalized);
    const modelKey = modelKeyFor(normalized.model);
    const preflightCost = pricing.price(modelKey, estimate.inputTokens, 0);

    const headers = filterRequestHeaders(req.raw.headers);
    headers["content-length"] = Buffer.byteLength(forwardBody);

    const target = new URL(req.url, upstream);
    target.protocol = upstream.protocol;
    target.host = upstream.host;

    const started = Date.now();
    const captured: Buffer[] = [];

    // Hand the socket over to us; Fastify will not try to send a response.
    reply.hijack();
    const out = reply.raw;

    const upstreamReq = client.request(
      target,
      { method: req.method, headers },
      (upstreamRes) => {
        // Surface the pre-flight estimate to programmatic consumers as response
        // headers (PRD §5.2). These are additive; the relayed body is untouched.
        const tollgateHeaders: http.OutgoingHttpHeaders = {
          "x-tollgate-input-tokens-est": String(estimate.inputTokens),
          "x-tollgate-input-accuracy": estimate.accuracy,
        };
        if (preflightCost) {
          tollgateHeaders["x-tollgate-est-input-cost-usd"] = preflightCost.inputCost.toFixed(6);
        } else {
          tollgateHeaders["x-tollgate-est-input-cost-usd"] = "unknown";
        }
        out.writeHead(upstreamRes.statusCode ?? 502, {
          ...upstreamRes.headers,
          ...tollgateHeaders,
        });

        upstreamRes.on("data", (chunk: Buffer) => {
          captured.push(chunk);
          // Stream straight through, honoring backpressure (CLAUDE.md §7).
          const ok = out.write(chunk);
          if (!ok) {
            upstreamRes.pause();
            out.once("drain", () => upstreamRes.resume());
          }
        });

        upstreamRes.on("end", () => {
          out.end();
          finalize(upstreamRes.headers, upstreamRes.statusCode ?? 0);
        });

        upstreamRes.on("error", () => {
          out.destroy();
        });
      },
    );

    upstreamReq.on("error", (err) => {
      if (!out.headersSent) {
        out.writeHead(502, { "content-type": "application/json" });
        out.end(JSON.stringify({ error: { type: "tollgate_upstream_error", message: String(err) } }));
      } else {
        out.destroy();
      }
    });

    upstreamReq.end(forwardBody);

    function finalize(
      resHeaders: http.IncomingHttpHeaders,
      _status: number,
    ): void {
      const upstreamMs = Date.now() - started;
      const usage = extractUsage(route.provider, Buffer.concat(captured), resHeaders);

      // Cost from actual usage where the provider reported it; fall back to the
      // local input estimate so a row always carries a best-effort cost.
      const inputForCost = usage.inputTokens ?? estimate.inputTokens;
      const outputForCost = usage.outputTokens ?? 0;
      const cost = pricing.price(modelKey, inputForCost, outputForCost);

      try {
        repo.insertRequest({
          id: randomUUID(),
          ts: started,
          provider: route.provider,
          model: normalized.model,
          routeLabel: route.label,
          inputTokensEst: estimate.inputTokens,
          inputTokensActual: usage.inputTokens ?? null,
          outputTokensActual: usage.outputTokens ?? null,
          estInputCost: cost?.inputCost ?? null,
          estOutputCost: cost?.outputCost ?? null,
          upstreamMs,
          contentHash: contentHash(normalized),
          rawLogged: route.rawLog,
        });
      } catch (err) {
        req.log.error({ err }, "failed to persist request record");
      }

      // Budget accounting runs after persistence so daily totals include this row.
      try {
        const totalTokens = inputForCost + outputForCost;
        const warnings = budget.record(totalTokens, cost?.total ?? 0);
        for (const w of warnings) {
          req.log.warn(
            { scope: w.scope, metric: w.metric, used: w.used, limit: w.limit, ratio: Number(w.ratio.toFixed(3)) },
            `budget ${w.scope} ${w.metric} crossed ${Math.round(w.threshold * 100)}%`,
          );
        }
      } catch (err) {
        req.log.error({ err }, "budget accounting failed");
      }
    }
  };
}

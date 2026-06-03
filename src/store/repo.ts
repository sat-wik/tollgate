import { readFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import Database from "better-sqlite3";
import { SCHEMA_PATH } from "../config/index.js";
import type { Finding, Severity } from "../lint/rules/types.js";

export type RequestRecord = {
  id: string;
  ts: number;
  provider: string;
  model: string;
  routeLabel: string | null;
  inputTokensEst: number | null;
  inputTokensActual: number | null;
  outputTokensActual: number | null;
  estInputCost: number | null;
  estOutputCost: number | null;
  upstreamMs: number | null;
  contentHash: string | null;
  rawLogged: boolean;
  requestType: string | null;
};

// Aggregate shapes for the read-only dashboard (M4).
export type Totals = {
  requests: number;
  inputTokens: number;
  outputTokens: number;
  cost: number;
  firstTs: number | null;
  lastTs: number | null;
};
export type Breakdown = { key: string; requests: number; tokens: number; cost: number };
export type TimeBucket = { day: string; requests: number; tokens: number; cost: number };
export type RequestWithFindings = RequestRecord & { findings: Finding[] };
export type Receipt = {
  request: RequestRecord;
  inputCost: number;
  outputCost: number;
  totalCost: number;
  topCostDriver: "input" | "output" | "unknown";
  findings: Finding[];
};

const TOKENS_SQL = `COALESCE(input_tokens_actual, input_tokens_est, 0) + COALESCE(output_tokens_actual, 0)`;
const COST_SQL = `COALESCE(est_input_cost, 0) + COALESCE(est_output_cost, 0)`;
// Whitelist of groupable columns — guards the dynamic GROUP BY against injection.
const BREAKDOWN_FIELDS = {
  model: "model",
  route: "route_label",
  provider: "provider",
  type: "request_type",
} as const;
export type BreakdownField = keyof typeof BREAKDOWN_FIELDS;

/** Typed access layer over the SQLite store. See CLAUDE.md §6.6. */
export class Repo {
  private db: Database.Database;

  constructor(storagePath: string) {
    if (storagePath !== ":memory:") {
      mkdirSync(dirname(storagePath), { recursive: true });
    }
    this.db = new Database(storagePath);
    this.db.pragma("journal_mode = WAL");
    this.db.exec(readFileSync(SCHEMA_PATH, "utf8"));
    this.migrate();
  }

  /** Additive migrations for stores created by an earlier schema. */
  private migrate(): void {
    const cols = this.db.prepare(`PRAGMA table_info(requests)`).all() as { name: string }[];
    if (!cols.some((c) => c.name === "request_type")) {
      this.db.exec(`ALTER TABLE requests ADD COLUMN request_type TEXT`);
    }
  }

  insertRequest(r: RequestRecord): void {
    this.db
      .prepare(
        `INSERT INTO requests (
           id, ts, provider, model, route_label,
           input_tokens_est, input_tokens_actual, output_tokens_actual,
           est_input_cost, est_output_cost, upstream_ms, content_hash, raw_logged,
           request_type
         ) VALUES (
           @id, @ts, @provider, @model, @routeLabel,
           @inputTokensEst, @inputTokensActual, @outputTokensActual,
           @estInputCost, @estOutputCost, @upstreamMs, @contentHash, @rawLogged,
           @requestType
         )`,
      )
      .run({
        ...r,
        rawLogged: r.rawLogged ? 1 : 0,
      });
  }

  getRequest(id: string): RequestRecord | undefined {
    const row = this.db.prepare(`SELECT * FROM requests WHERE id = ?`).get(id) as
      | Record<string, unknown>
      | undefined;
    return row ? rowToRecord(row) : undefined;
  }

  recentRequests(limit = 100): RequestRecord[] {
    const rows = this.db
      .prepare(`SELECT * FROM requests ORDER BY ts DESC LIMIT ?`)
      .all(limit) as Record<string, unknown>[];
    return rows.map(rowToRecord);
  }

  insertFindings(requestId: string, findings: Finding[]): void {
    if (!findings.length) return;
    const stmt = this.db.prepare(
      `INSERT INTO findings (request_id, rule, severity, tokens_wasted_est, message, location_json)
       VALUES (?, ?, ?, ?, ?, ?)`,
    );
    const tx = this.db.transaction((items: Finding[]) => {
      for (const f of items) {
        stmt.run(
          requestId,
          f.rule,
          f.severity,
          Math.round(f.tokensWastedEst),
          f.message,
          f.location ? JSON.stringify(f.location) : null,
        );
      }
    });
    tx(findings);
  }

  getFindings(requestId: string): Finding[] {
    const rows = this.db
      .prepare(`SELECT * FROM findings WHERE request_id = ?`)
      .all(requestId) as Record<string, unknown>[];
    return rows.map((row) => ({
      rule: row.rule as string,
      severity: row.severity as Severity,
      tokensWastedEst: row.tokens_wasted_est as number,
      message: row.message as string,
      location: row.location_json
        ? (JSON.parse(row.location_json as string) as Finding["location"])
        : undefined,
    }));
  }

  /** Summed token + cost totals for requests at or after `sinceTs`. */
  totalsSince(sinceTs: number): { tokens: number; cost: number } {
    const row = this.db
      .prepare(
        `SELECT
           COALESCE(SUM(COALESCE(input_tokens_actual, 0) + COALESCE(output_tokens_actual, 0)), 0) AS tokens,
           COALESCE(SUM(COALESCE(est_input_cost, 0) + COALESCE(est_output_cost, 0)), 0) AS cost
         FROM requests WHERE ts >= ?`,
      )
      .get(sinceTs) as { tokens: number; cost: number };
    return { tokens: row.tokens, cost: row.cost };
  }

  // --- Dashboard read models (M4) --------------------------------------------

  summary(): Totals {
    const row = this.db
      .prepare(
        `SELECT
           COUNT(*) AS requests,
           COALESCE(SUM(COALESCE(input_tokens_actual, input_tokens_est, 0)), 0) AS inputTokens,
           COALESCE(SUM(COALESCE(output_tokens_actual, 0)), 0) AS outputTokens,
           COALESCE(SUM(${COST_SQL}), 0) AS cost,
           MIN(ts) AS firstTs,
           MAX(ts) AS lastTs
         FROM requests`,
      )
      .get() as Totals;
    return row;
  }

  breakdownBy(field: BreakdownField): Breakdown[] {
    const column = BREAKDOWN_FIELDS[field];
    return this.db
      .prepare(
        `SELECT
           COALESCE(${column}, 'unknown') AS key,
           COUNT(*) AS requests,
           COALESCE(SUM(${TOKENS_SQL}), 0) AS tokens,
           COALESCE(SUM(${COST_SQL}), 0) AS cost
         FROM requests GROUP BY ${column} ORDER BY cost DESC`,
      )
      .all() as Breakdown[];
  }

  spendOverTime(): TimeBucket[] {
    return this.db
      .prepare(
        `SELECT
           date(ts / 1000, 'unixepoch') AS day,
           COUNT(*) AS requests,
           COALESCE(SUM(${TOKENS_SQL}), 0) AS tokens,
           COALESCE(SUM(${COST_SQL}), 0) AS cost
         FROM requests GROUP BY day ORDER BY day ASC`,
      )
      .all() as TimeBucket[];
  }

  recentWithFindings(limit = 50): RequestWithFindings[] {
    return this.recentRequests(limit).map((r) => ({ ...r, findings: this.getFindings(r.id) }));
  }

  receipt(id: string): Receipt | undefined {
    const request = this.getRequest(id);
    if (!request) return undefined;
    const inputCost = request.estInputCost ?? 0;
    const outputCost = request.estOutputCost ?? 0;
    const priced = request.estInputCost != null || request.estOutputCost != null;
    const topCostDriver: Receipt["topCostDriver"] = !priced
      ? "unknown"
      : outputCost > inputCost
        ? "output"
        : "input";
    return {
      request,
      inputCost,
      outputCost,
      totalCost: inputCost + outputCost,
      topCostDriver,
      findings: this.getFindings(id),
    };
  }

  close(): void {
    this.db.close();
  }
}

function rowToRecord(row: Record<string, unknown>): RequestRecord {
  return {
    id: row.id as string,
    ts: row.ts as number,
    provider: row.provider as string,
    model: row.model as string,
    routeLabel: (row.route_label as string) ?? null,
    inputTokensEst: (row.input_tokens_est as number) ?? null,
    inputTokensActual: (row.input_tokens_actual as number) ?? null,
    outputTokensActual: (row.output_tokens_actual as number) ?? null,
    estInputCost: (row.est_input_cost as number) ?? null,
    estOutputCost: (row.est_output_cost as number) ?? null,
    upstreamMs: (row.upstream_ms as number) ?? null,
    contentHash: (row.content_hash as string) ?? null,
    rawLogged: Boolean(row.raw_logged),
    requestType: (row.request_type as string) ?? null,
  };
}

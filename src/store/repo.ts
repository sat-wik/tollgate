import { readFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import Database from "better-sqlite3";
import { SCHEMA_PATH } from "../config/index.js";

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
};

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
  }

  insertRequest(r: RequestRecord): void {
    this.db
      .prepare(
        `INSERT INTO requests (
           id, ts, provider, model, route_label,
           input_tokens_est, input_tokens_actual, output_tokens_actual,
           est_input_cost, est_output_cost, upstream_ms, content_hash, raw_logged
         ) VALUES (
           @id, @ts, @provider, @model, @routeLabel,
           @inputTokensEst, @inputTokensActual, @outputTokensActual,
           @estInputCost, @estOutputCost, @upstreamMs, @contentHash, @rawLogged
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
  };
}

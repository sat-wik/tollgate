import type { FastifyInstance } from "fastify";
import type { Repo } from "../../store/repo.js";
import { dashboardHtml } from "./page.js";

/**
 * Registers the read-only Tollgate dashboard + receipt endpoints under the
 * /_tollgate prefix (distinct from the provider API routes). Everything here is
 * a thin viewer over the store — no writes, no upstream calls, fully offline
 * (CLAUDE.md §2). Raw prompt content is never exposed; only stored metadata,
 * costs, and findings.
 */
export function registerDashboard(app: FastifyInstance, repo: Repo): void {
  app.get("/_tollgate", async (_req, reply) => {
    reply.type("text/html").send(dashboardHtml());
  });

  app.get("/_tollgate/api/summary", async () => ({
    summary: repo.summary(),
    byModel: repo.breakdownBy("model"),
    byRoute: repo.breakdownBy("route"),
    byType: repo.breakdownBy("type"),
    spendOverTime: repo.spendOverTime(),
  }));

  app.get("/_tollgate/api/requests", async (req) => {
    const limit = Number((req.query as { limit?: string }).limit ?? 50);
    return repo.recentWithFindings(Number.isFinite(limit) ? Math.min(limit, 500) : 50);
  });

  app.get("/_tollgate/receipt/:id", async (req, reply) => {
    const { id } = req.params as { id: string };
    const receipt = repo.receipt(id);
    if (!receipt) {
      reply.code(404).send({ error: { type: "not_found", message: `no request ${id}` } });
      return;
    }
    return receipt;
  });
}

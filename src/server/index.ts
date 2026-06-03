import Fastify, { type FastifyInstance } from "fastify";
import type { Config } from "../config/index.js";
import { Repo } from "../store/repo.js";
import { createProxyHandler } from "./proxy.js";

export type TollgateServer = {
  app: FastifyInstance;
  repo: Repo;
};

export function buildServer(config: Config, opts: { logger?: boolean } = {}): TollgateServer {
  const app = Fastify({
    logger: opts.logger ?? false,
    // We forward bodies byte-for-byte, so cap is generous and configurable later.
    bodyLimit: 64 * 1024 * 1024,
  });

  // Capture the raw request body for byte-equivalent forwarding. We parse JSON
  // ourselves in the handler so the exact bytes are preserved.
  app.removeAllContentTypeParsers();
  app.addContentTypeParser("*", { parseAs: "buffer" }, (_req, body, done) => {
    done(null, body);
  });

  const repo = new Repo(config.storagePath);

  for (const route of config.routes) {
    app.post(route.path, createProxyHandler(route, repo));
  }

  // Liveness probe (not a provider route).
  app.get("/_tollgate/health", async () => ({ status: "ok", routes: config.routes.length }));

  app.addHook("onClose", async () => {
    repo.close();
  });

  return { app, repo };
}

#!/usr/bin/env node
import { loadConfig } from "./config/index.js";
import { buildServer } from "./server/index.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const { app } = buildServer(config, { logger: true });

  await app.listen({ port: config.port, host: "127.0.0.1" });

  app.log.info(
    { port: config.port, routes: config.routes.map((r) => `${r.path} -> ${r.upstream}`) },
    "tollgate listening",
  );

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      app.log.info(`received ${signal}, shutting down`);
      app.close().then(() => process.exit(0));
    });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

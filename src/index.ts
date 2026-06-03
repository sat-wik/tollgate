#!/usr/bin/env node
import { loadConfig } from "./config/index.js";
import { buildServer } from "./server/index.js";
import { runInit } from "./cli/init.js";

const HELP = `tollgate — a local, provider-agnostic LLM cost proxy

Usage:
  tollgate [serve]   Start the proxy (default command)
  tollgate init      Scaffold ~/.tollgate/config.toml and print setup steps
  tollgate --help    Show this help

Once running, point your tools at the proxy's base URL (e.g.
ANTHROPIC_BASE_URL / OPENAI_BASE_URL) and open the dashboard at /_tollgate.`;

async function serve(): Promise<void> {
  const config = loadConfig();
  const { app } = buildServer(config, { logger: true });

  await app.listen({ port: config.port, host: "127.0.0.1" });

  app.log.info(
    {
      port: config.port,
      dashboard: `http://127.0.0.1:${config.port}/_tollgate`,
      routes: config.routes.map((r) => `${r.path} -> ${r.upstream}`),
    },
    "tollgate listening",
  );

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      app.log.info(`received ${signal}, shutting down`);
      app.close().then(() => process.exit(0));
    });
  }
}

async function main(): Promise<void> {
  const cmd = process.argv[2];
  switch (cmd) {
    case "init":
      runInit();
      return;
    case "-h":
    case "--help":
    case "help":
      console.log(HELP);
      return;
    case undefined:
    case "serve":
      await serve();
      return;
    default:
      console.error(`Unknown command: ${cmd}\n`);
      console.error(HELP);
      process.exit(2);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

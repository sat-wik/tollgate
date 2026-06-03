import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import TOML from "@iarna/toml";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG = join(__dirname, "..", "..", "config", "default.toml");

export type InitResult = { configPath: string; created: boolean; port: number };

/**
 * `tollgate init` — scaffold a user config and print setup steps. Idempotent:
 * an existing config is left untouched (never clobbered).
 */
export function runInit(opts: { home?: string; print?: (s: string) => void } = {}): InitResult {
  const home = opts.home ?? homedir();
  const print = opts.print ?? ((s: string) => console.log(s));
  const dir = join(home, ".tollgate");
  const configPath = join(dir, "config.toml");

  mkdirSync(dir, { recursive: true });

  let created = false;
  if (existsSync(configPath)) {
    print(`✓ Config already exists at ${configPath} (left unchanged).`);
  } else {
    copyFileSync(DEFAULT_CONFIG, configPath);
    created = true;
    print(`✓ Created ${configPath}`);
  }

  const parsed = TOML.parse(readFileSync(configPath, "utf8")) as { port?: number };
  const port = parsed.port ?? 8787;
  const base = `http://127.0.0.1:${port}`;

  print("");
  print("Next steps:");
  print(`  1. Start the proxy:`);
  print(`       npx tollgate            (or: npm start)`);
  print(`  2. Point your tools at it:`);
  print(`       Claude Code / Anthropic SDK:  export ANTHROPIC_BASE_URL=${base}`);
  print(`       OpenAI SDK:                   export OPENAI_BASE_URL=${base}/v1`);
  print(`  3. Open the dashboard:`);
  print(`       ${base}/_tollgate`);
  print("");
  print("Your API keys are passed straight through to the provider and are never");
  print("stored. By default only request metadata + content hashes are persisted");
  print("(no raw prompts). Edit the config to tune budgets, thresholds, and pricing.");

  return { configPath, created, port };
}

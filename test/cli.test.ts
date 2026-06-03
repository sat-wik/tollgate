import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { runInit } from "../src/cli/init.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG = join(__dirname, "..", "config", "default.toml");

let home: string;
function capture() {
  const lines: string[] = [];
  return { print: (s: string) => lines.push(s), lines };
}

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "tollgate-cli-"));
});
afterEach(() => {
  rmSync(home, { recursive: true, force: true });
});

describe("tollgate init", () => {
  it("creates ~/.tollgate/config.toml from the bundled default", () => {
    const out = capture();
    const res = runInit({ home, print: out.print });

    expect(res.created).toBe(true);
    expect(res.configPath).toBe(join(home, ".tollgate", "config.toml"));
    expect(existsSync(res.configPath)).toBe(true);
    expect(readFileSync(res.configPath, "utf8")).toBe(readFileSync(DEFAULT_CONFIG, "utf8"));
  });

  it("prints actionable setup steps with the base URL", () => {
    const out = capture();
    runInit({ home, print: out.print });
    const text = out.lines.join("\n");
    expect(text).toContain("ANTHROPIC_BASE_URL=http://127.0.0.1:8787");
    expect(text).toContain("OPENAI_BASE_URL=http://127.0.0.1:8787/v1");
    expect(text).toContain("/_tollgate");
  });

  it("is idempotent: a second run does not overwrite an existing config", () => {
    runInit({ home, print: () => {} });
    const out = capture();
    const res = runInit({ home, print: out.print });
    expect(res.created).toBe(false);
    expect(out.lines.join("\n")).toContain("left unchanged");
  });
});

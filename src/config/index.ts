import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import TOML from "@iarna/toml";
import type { Provider } from "../adapters/types.js";
import type { PriceTable } from "../pricing/index.js";
import type { BudgetLimits } from "../budget/tracker.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG_PATH = join(__dirname, "..", "..", "config", "default.toml");

export type RouteConfig = {
  provider: Provider;
  path: string;
  upstream: string;
  label: string;
  rawLog: boolean;
  injectUsage: boolean;
};

export type Config = {
  port: number;
  storagePath: string;
  routes: RouteConfig[];
  budget: BudgetLimits;
  pricingOverrides: PriceTable;
};

/** Expand a leading "~" to the user's home directory. Leaves ":memory:" intact. */
export function expandHome(p: string): string {
  if (p === ":memory:") return p;
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return join(homedir(), p.slice(2));
  return p;
}

type RawRoute = {
  provider?: string;
  path?: string;
  upstream?: string;
  label?: string;
  raw_log?: boolean;
  inject_usage?: boolean;
};

type RawBudget = {
  session_tokens?: number;
  session_cost?: number;
  daily_tokens?: number;
  daily_cost?: number;
  thresholds?: number[];
  block?: boolean;
};

type RawConfig = {
  port?: number;
  storage?: { path?: string };
  routes?: Record<string, RawRoute>;
  budget?: RawBudget;
  pricing?: { overrides?: Record<string, Record<string, unknown>> };
};

function parseRoutes(raw: Record<string, RawRoute> | undefined): RouteConfig[] {
  if (!raw) return [];
  return Object.entries(raw).map(([name, r]) => {
    const provider = r.provider as Provider | undefined;
    if (provider !== "anthropic" && provider !== "openai") {
      throw new Error(`route "${name}": provider must be "anthropic" or "openai"`);
    }
    if (!r.path || !r.upstream) {
      throw new Error(`route "${name}": "path" and "upstream" are required`);
    }
    return {
      provider,
      path: r.path,
      upstream: r.upstream.replace(/\/$/, ""),
      label: r.label ?? name,
      rawLog: r.raw_log ?? false,
      injectUsage: r.inject_usage ?? false,
    };
  });
}

function parseBudget(raw: RawBudget | undefined): BudgetLimits {
  return {
    sessionTokens: raw?.session_tokens,
    sessionCost: raw?.session_cost,
    dailyTokens: raw?.daily_tokens,
    dailyCost: raw?.daily_cost,
    thresholds: raw?.thresholds ?? [0.8, 1.0],
    block: raw?.block ?? false,
  };
}

function parsePricingOverrides(
  raw: Record<string, Record<string, unknown>> | undefined,
): PriceTable {
  const table: PriceTable = {};
  if (!raw) return table;
  for (const [key, v] of Object.entries(raw)) {
    if (typeof v.inputPerMTok === "number" && typeof v.outputPerMTok === "number") {
      table[key] = {
        inputPerMTok: v.inputPerMTok,
        outputPerMTok: v.outputPerMTok,
        cachedInputPerMTok:
          typeof v.cachedInputPerMTok === "number" ? v.cachedInputPerMTok : undefined,
      };
    }
  }
  return table;
}

function mergeRaw(base: RawConfig, over: RawConfig): RawConfig {
  return {
    port: over.port ?? base.port,
    storage: { path: over.storage?.path ?? base.storage?.path },
    // A user-provided [routes] table replaces the default set wholesale, so users
    // can change upstreams/labels without inheriting defaults they didn't ask for.
    routes: over.routes ?? base.routes,
    budget: { ...base.budget, ...over.budget },
    pricing: { overrides: { ...base.pricing?.overrides, ...over.pricing?.overrides } },
  };
}

export type LoadOptions = {
  /** Path to the user config. Defaults to ~/.tollgate/config.toml. */
  userConfigPath?: string;
  /** Inline overrides applied last (used by tests). */
  overrides?: Partial<Config>;
};

export function loadConfig(opts: LoadOptions = {}): Config {
  const defaults = TOML.parse(readFileSync(DEFAULT_CONFIG_PATH, "utf8")) as RawConfig;

  const userPath = opts.userConfigPath ?? join(homedir(), ".tollgate", "config.toml");
  let raw = defaults;
  if (existsSync(userPath)) {
    const user = TOML.parse(readFileSync(userPath, "utf8")) as RawConfig;
    raw = mergeRaw(defaults, user);
  }

  const config: Config = {
    port: raw.port ?? 8787,
    storagePath: expandHome(raw.storage?.path ?? "~/.tollgate/tollgate.db"),
    routes: parseRoutes(raw.routes),
    budget: parseBudget(raw.budget),
    pricingOverrides: parsePricingOverrides(raw.pricing?.overrides),
  };

  if (opts.overrides) {
    Object.assign(config, opts.overrides);
    if (opts.overrides.storagePath) {
      config.storagePath = expandHome(opts.overrides.storagePath);
    }
  }

  return config;
}

export const SCHEMA_PATH = resolve(__dirname, "..", "store", "schema.sql");

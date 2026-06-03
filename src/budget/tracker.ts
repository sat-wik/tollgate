// Per-session and per-day budget tracking with threshold warnings. See PRD §5.5
// and CLAUDE.md M2. v1 policy: 100% produces a prominent warning but does NOT
// block unless `block` is explicitly enabled (default off).

export type BudgetLimits = {
  sessionTokens?: number;
  sessionCost?: number;
  dailyTokens?: number;
  dailyCost?: number;
  thresholds: number[]; // fractions, e.g. [0.8, 1.0]
  block: boolean;
};

export type BudgetScope = "session" | "daily";
export type BudgetMetric = "tokens" | "cost";

export type BudgetWarning = {
  scope: BudgetScope;
  metric: BudgetMetric;
  threshold: number; // the fraction that was crossed (e.g. 0.8)
  limit: number;
  used: number;
  ratio: number;
};

export type DailyTotals = { tokens: number; cost: number };

/** Start-of-day (local time) epoch ms — used to scope "daily" totals. */
export function startOfDay(now = Date.now()): number {
  const d = new Date(now);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export class BudgetTracker {
  private sessionTokens = 0;
  private sessionCost = 0;
  private firedSession = new Set<string>();
  private firedDaily = new Set<string>();
  private day = startOfDay();

  /**
   * @param getDailyTotals returns persisted totals for the current day. The
   *   caller should persist the request BEFORE calling record(), so daily totals
   *   include it.
   */
  constructor(
    private limits: BudgetLimits,
    private getDailyTotals: (sinceTs: number) => DailyTotals,
  ) {}

  /** Record a completed request's usage; returns any newly-crossed thresholds. */
  record(tokens: number, cost: number): BudgetWarning[] {
    const today = startOfDay();
    if (today !== this.day) {
      this.day = today;
      this.firedDaily.clear();
    }

    this.sessionTokens += tokens;
    this.sessionCost += cost;
    const daily = this.getDailyTotals(this.day);

    const warnings: BudgetWarning[] = [];
    const sorted = [...this.limits.thresholds].sort((a, b) => a - b);

    const checkScope = (
      scope: BudgetScope,
      fired: Set<string>,
      tokenUsed: number,
      tokenLimit: number | undefined,
      costUsed: number,
      costLimit: number | undefined,
    ): void => {
      this.check(scope, "tokens", tokenUsed, tokenLimit, fired, sorted, warnings);
      this.check(scope, "cost", costUsed, costLimit, fired, sorted, warnings);
    };

    checkScope(
      "session",
      this.firedSession,
      this.sessionTokens,
      this.limits.sessionTokens,
      this.sessionCost,
      this.limits.sessionCost,
    );
    checkScope(
      "daily",
      this.firedDaily,
      daily.tokens,
      this.limits.dailyTokens,
      daily.cost,
      this.limits.dailyCost,
    );

    return warnings;
  }

  /** Whether v1 blocking is enabled and any limit is fully exhausted. */
  get blockingEnabled(): boolean {
    return this.limits.block;
  }

  private check(
    scope: BudgetScope,
    metric: BudgetMetric,
    used: number,
    limit: number | undefined,
    fired: Set<string>,
    thresholds: number[],
    out: BudgetWarning[],
  ): void {
    if (!limit || limit <= 0) return;
    const ratio = used / limit;
    for (const t of thresholds) {
      if (ratio < t) continue;
      const key = `${scope}:${metric}:${t}`;
      if (fired.has(key)) continue;
      fired.add(key);
      out.push({ scope, metric, threshold: t, limit, used, ratio });
    }
  }
}

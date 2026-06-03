import { describe, expect, it } from "vitest";
import { BudgetTracker, type BudgetLimits, type DailyTotals } from "../src/budget/tracker.js";

const noDaily = (): DailyTotals => ({ tokens: 0, cost: 0 });

function tracker(limits: Partial<BudgetLimits>, daily: () => DailyTotals = noDaily): BudgetTracker {
  return new BudgetTracker(
    { thresholds: [0.8, 1.0], block: false, ...limits },
    daily,
  );
}

describe("BudgetTracker", () => {
  it("fires a warning when session tokens cross 80% then 100%", () => {
    const t = tracker({ sessionTokens: 100 });

    expect(t.record(50, 0)).toHaveLength(0); // 50%
    const w80 = t.record(35, 0); // 85%
    expect(w80).toHaveLength(1);
    expect(w80[0]).toMatchObject({ scope: "session", metric: "tokens", threshold: 0.8 });

    const w100 = t.record(20, 0); // 105%
    expect(w100).toHaveLength(1);
    expect(w100[0].threshold).toBe(1.0);
  });

  it("does not re-fire a threshold already crossed", () => {
    const t = tracker({ sessionTokens: 100 });
    t.record(100, 0); // crosses both 0.8 and 1.0 at once
    expect(t.record(50, 0)).toHaveLength(0);
  });

  it("crossing 0.8 and 1.0 simultaneously yields two warnings", () => {
    const t = tracker({ sessionTokens: 100 });
    const w = t.record(100, 0);
    expect(w.map((x) => x.threshold).sort()).toEqual([0.8, 1.0]);
  });

  it("tracks session cost independently of tokens", () => {
    const t = tracker({ sessionCost: 10 });
    expect(t.record(999999, 8)).toHaveLength(1); // 80% of cost; tokens have no limit
    const w = t.record(0, 2); // 100% of cost
    expect(w).toHaveLength(1);
    expect(w[0]).toMatchObject({ scope: "session", metric: "cost", threshold: 1.0 });
  });

  it("uses persisted daily totals from the callback", () => {
    let daily: DailyTotals = { tokens: 0, cost: 0 };
    const t = tracker({ dailyTokens: 1000 }, () => daily);
    daily = { tokens: 850, cost: 0 }; // store already reflects this request
    const w = t.record(850, 0);
    expect(w).toHaveLength(1);
    expect(w[0]).toMatchObject({ scope: "daily", metric: "tokens", threshold: 0.8 });
  });

  it("ignores unset/zero limits", () => {
    const t = tracker({});
    expect(t.record(1_000_000, 1000)).toHaveLength(0);
  });

  it("exposes the v1 blocking flag (default off)", () => {
    expect(tracker({}).blockingEnabled).toBe(false);
    expect(tracker({ block: true }).blockingEnabled).toBe(true);
  });
});

import { describe, expect, it } from "vitest";
import { evaluate, isCustomerRoute } from "../scripts/bundle-check.mjs";

describe("the per-route bundle check", () => {
  it("passes routes within budget and within 2 KB of the baseline", () => {
    expect(evaluate({ "/": 143.3, "/login": 148.9 }, { "/": 143.0, "/login": 147.0 }).failures).toEqual([]);
  });

  it("fails a customer route over 200 KB", () => {
    const { failures } = evaluate({ "/b/[id]/book": 200.4 }, { "/b/[id]/book": 199.9 });
    expect(failures).toEqual(["/b/[id]/book is 200.4 KB, over the 200 KB budget"]);
  });

  it("fails a route that drifted more than 2 KB from the baseline, either way", () => {
    expect(evaluate({ "/login": 149.1 }, { "/login": 147.0 }).failures).toHaveLength(1);
    expect(evaluate({ "/login": 144.9 }, { "/login": 147.0 }).failures).toHaveLength(1);
  });

  it("fails routes missing from the baseline and baseline routes no longer built", () => {
    expect(evaluate({ "/new": 150 }, {}).failures).toEqual([
      "/new is not in the baseline (add it with --write-baseline)",
    ]);
    expect(evaluate({}, { "/gone": 150 }).failures).toEqual(["/gone is in the baseline but no longer built"]);
  });

  it("applies the 200 KB budget to customer routes only", () => {
    expect(["/", "/login", "/me", "/b", "/b/[id]", "/b/[id]/book"].every(isCustomerRoute)).toBe(true);
    expect(["/owner", "/console", "/signin", "/register", "/bx"].some(isCustomerRoute)).toBe(false);
  });
});

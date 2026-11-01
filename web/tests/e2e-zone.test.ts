import { describe, expect, it } from "vitest";
import { hourIn, queueZone } from "../e2e/zone";

describe("the e2e queue zone", () => {
  it("is never within 2 hours of local midnight, at any time of day (every 15 minutes)", () => {
    const start = Date.UTC(2026, 10, 2); // a Monday; Kolkata has no DST, the fallbacks do
    for (let minutes = 0; minutes < 24 * 60; minutes += 15) {
      const now = new Date(start + minutes * 60_000);
      const { zone, why } = queueZone(now);
      const hour = hourIn(zone, now);
      expect(hour >= 2 && hour < 22, `${now.toISOString()} → ${zone} at ${hour}:xx (${why})`).toBe(true);
    }
  });

  it("keeps Kolkata whenever Kolkata itself is far enough from midnight", () => {
    expect(queueZone(new Date("2026-11-02T06:30:00Z")).zone).toBe("Asia/Kolkata"); // noon there
    expect(queueZone(new Date("2026-11-02T18:45:00Z")).zone).not.toBe("Asia/Kolkata"); // 00:15 there
  });
});

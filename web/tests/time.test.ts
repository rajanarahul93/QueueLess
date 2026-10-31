import { describe, expect, it } from "vitest";
import { addDays, dayButtonName, dayLabel, localToday, timeLabel } from "@/lib/time";

describe("business-zone time", () => {
  it("runs under a Los Angeles browser zone (the setup), so these checks mean something", () => {
    expect(new Date("2026-11-02T12:00:00Z").getTimezoneOffset()).toBe(480); // PST
  });

  it("labels an API instant in the business zone, not the browser's", () => {
    expect(timeLabel("2026-11-02T09:00:00+05:30", "Asia/Kolkata")).toBe("9:00 AM");
    expect(timeLabel("2026-11-02T03:30:00Z", "Asia/Kolkata")).toBe("9:00 AM"); // same instant in UTC
  });

  it("labels New York's DST days by the wall clock there", () => {
    // 2026-03-08: 02:00 does not exist; the slot after 01:30 EST is 03:00 EDT
    expect(timeLabel("2026-03-08T01:30:00-05:00", "America/New_York")).toBe("1:30 AM");
    expect(timeLabel("2026-03-08T03:00:00-04:00", "America/New_York")).toBe("3:00 AM");
    // 2026-11-01: 01:30 happens twice (EDT, then EST)
    expect(timeLabel("2026-11-01T01:30:00-04:00", "America/New_York")).toBe("1:30 AM");
    expect(timeLabel("2026-11-01T01:30:00-05:00", "America/New_York")).toBe("1:30 AM");
    expect(timeLabel("2026-11-01T02:00:00-05:00", "America/New_York")).toBe("2:00 AM");
  });

  it("takes today from the business zone", () => {
    // 20:00 in Los Angeles on 1 Nov is already 2 Nov in Kolkata
    const now = new Date("2026-11-02T03:00:00Z");
    expect(localToday("Asia/Kolkata", now)).toBe("2026-11-02");
    expect(localToday("America/Los_Angeles", now)).toBe("2026-11-01");
  });

  it("does calendar arithmetic without any zone", () => {
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-08", 1)).toBe("2026-03-09"); // a DST day is still one day
    expect(dayLabel("2026-11-02", "long")).toBe("Mon, Nov 2");
    expect(dayLabel("2026-11-02", "weekday")).toBe("Mon");
    expect(dayButtonName("2026-11-02")).toBe("Mon 2 Nov");
  });
});

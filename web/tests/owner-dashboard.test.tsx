import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/owner/b1/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

const { OwnerDashboard } = await import("@/components/owner/OwnerDashboard");
const { byDay, byHour, range, summary, minutes, percent } = await import("@/lib/owner/reports");

const ZONE = "Asia/Kolkata";
let peaks: { weekday: number; hour: number; tokens: number }[];
let waits: { date: string; service_id: string; served: number; avg_wait_sec: number; p90_wait_sec: number | null; avg_predicted_wait_sec: number | null }[];
let misses: { service_id: string | null; joined: number; no_shows: number; cancelled: number; no_show_rate: number; cancel_rate: number }[];
let asked: string[];
let fail: number | null;

function serve() {
  asked = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      asked.push(String(url));
      if (fail !== null) return Response.json({ code: fail === 429 ? "RATE_LIMITED" : "SERVICE_UNAVAILABLE" }, { status: fail });
      if (url.includes("peak-hours")) return Response.json(peaks);
      if (url.includes("wait-times")) return Response.json(waits);
      if (url.includes("no-shows")) return Response.json(misses);
      return Response.json({ id: "b1", name: "North Clinic", category: "clinic", address: null, timezone: ZONE, open_hours: {} });
    }),
  );
}

const show = (node: ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);

beforeEach(() => {
  fail = null;
  peaks = [
    { weekday: 0, hour: 10, tokens: 4 },
    { weekday: 1, hour: 10, tokens: 5 },
    { weekday: 1, hour: 17, tokens: 12 },
  ];
  waits = [
    { date: "2026-11-09", service_id: "s1", served: 4, avg_wait_sec: 600, p90_wait_sec: 1200, avg_predicted_wait_sec: 540 },
    { date: "2026-11-09", service_id: "s2", served: 6, avg_wait_sec: 300, p90_wait_sec: 900, avg_predicted_wait_sec: 300 },
    { date: "2026-11-10", service_id: "s1", served: 5, avg_wait_sec: 1200, p90_wait_sec: null, avg_predicted_wait_sec: null },
  ];
  misses = [
    { service_id: "s1", joined: 20, no_shows: 2, cancelled: 1, no_show_rate: 0.1, cancel_rate: 0.05 },
    { service_id: null, joined: 10, no_shows: 1, cancelled: 0, no_show_rate: 0.1, cancel_rate: 0 },
  ];
  serve();
});
afterEach(() => vi.unstubAllGlobals());

describe("the report figures", () => {
  it("ends yesterday in the business's own zone, over 30 days", () => {
    // 1 January in Kolkata is still 31 December in UTC: the range must follow the business.
    const span = range(ZONE, new Date("2026-01-01T02:00:00+05:30"));
    expect(span.to).toBe("2025-12-31");
    expect(span.from).toBe("2025-12-02");
  });

  it("keeps every hour of the day, so the bars line up", () => {
    const hours = byHour(peaks);
    expect(hours).toHaveLength(24);
    expect(hours[10].tokens).toBe(9); // both weekdays at 10:00
    expect(hours[17].tokens).toBe(12);
    expect(hours[0].tokens).toBe(0);
  });

  it("pools a day's services by the tokens they served, and keeps the worst p90", () => {
    const days = byDay(waits);
    expect(days.map((day) => day.date)).toEqual(["2026-11-09", "2026-11-10"]);
    expect(days[0].served).toBe(10);
    expect(days[0].avg).toBe(420); // (600*4 + 300*6) / 10
    expect(days[0].p90).toBe(1200);
    expect(days[1].p90).toBeNull(); // under five waits: the API sends null
  });

  it("totals the range for the sentences at the top", () => {
    const totals = summary(peaks, waits, misses);
    expect(totals.busiestHour).toBe(17);
    expect(totals.served).toBe(15);
    expect(totals.avgWaitSec).toBe(680); // (600*4 + 300*6 + 1200*5) / 15
    expect(totals.joined).toBe(30);
    expect(totals.noShows).toBe(3);
    expect(percent(totals.noShowRate)).toBe("10%");
    expect(minutes(680)).toBe(11);
  });

  it("an empty range has no busiest hour", () => {
    expect(summary([], [], []).busiestHour).toBeNull();
  });
});

describe("the owner dashboard", () => {
  it("says the range ends yesterday", async () => {
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText(/^Up to yesterday · /)).toBeTruthy();
    expect(asked.some((url) => url.includes("peak-hours?from="))).toBe(true);
  });

  it("shows the three summaries", async () => {
    show(<OwnerDashboard businessId="b1" />);
    // Scoped to the three tiles: the same numbers also appear in the chart tables below.
    const tiles = (await screen.findByText("Busiest hour")).closest("ul")!;
    expect(within(tiles).getByText("17:00")).toBeTruthy();
    expect(within(tiles).getByText("12 joined")).toBeTruthy();
    expect(within(tiles).getByText("11 min")).toBeTruthy();
    expect(within(tiles).getByText("over 15 served")).toBeTruthy();
    expect(within(tiles).getByText("10%")).toBeTruthy();
    expect(within(tiles).getByText("3 of 30 joined")).toBeTruthy();
  });

  it("every chart carries the same numbers as a table", async () => {
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText("When people join")).toBeTruthy();
    const views = screen.getAllByRole("group"); // the <details> blocks
    expect(views.length).toBeGreaterThanOrEqual(2);
    await userEvent.click(screen.getAllByText("View data")[0]);
    const joined = screen.getByRole("table", { name: "Tokens joined" });
    expect(within(joined).getByRole("rowheader", { name: "17:00" })).toBeTruthy();
    expect(within(joined).getByRole("row", { name: "17:00 12" })).toBeTruthy();
    // the charts are labelled, not left as bare pictures
    for (const picture of screen.getAllByRole("img")) {
      expect(picture.getAttribute("aria-label")).toContain("The same numbers are in the table below.");
    }
  });

  it("the wait chart plots the average per day, and p90 has its own table", async () => {
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText("How long they waited")).toBeTruthy();
    await userEvent.click(screen.getAllByText("View data")[1]);
    const table = screen.getByRole("table", { name: "Average wait (minutes)" });
    expect(within(table).getByRole("row", { name: "2026-11-09 7" })).toBeTruthy(); // 420 s
    expect(within(table).getByRole("row", { name: "2026-11-10 20" })).toBeTruthy(); // 1200 s
    await userEvent.click(screen.getByText("View the slowest tenth too"));
    const p90 = screen.getByRole("table", { name: /p90 wait/ });
    expect(within(p90).getByRole("row", { name: "2026-11-09 20 10" })).toBeTruthy();
    expect(within(p90).getByRole("row", { name: "2026-11-10 — 5" })).toBeTruthy();
  });

  it("no-shows are listed by service, with walk-ins named", async () => {
    show(<OwnerDashboard businessId="b1" />);
    const table = await screen.findByRole("table", { name: "By service over the range" });
    expect(within(table).getByRole("rowheader", { name: "Walk-ins with no service" })).toBeTruthy();
    expect(within(table).getAllByRole("row")).toHaveLength(3); // the header and two services
  });

  it("a business with no finished days says so", async () => {
    peaks = [];
    waits = [];
    misses = [];
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText(/No data yet\./)).toBeTruthy();
    expect(screen.queryByText("When people join")).toBeNull();
  });

  it("a failure offers Retry", async () => {
    fail = 503;
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText("We can't do that right now. Please try again in a moment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("too many reads in a minute is explained", async () => {
    fail = 429;
    show(<OwnerDashboard businessId="b1" />);
    expect(await screen.findByText("Too many attempts. Please wait and try again.")).toBeTruthy();
  });
});

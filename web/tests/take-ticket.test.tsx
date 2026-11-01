import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
let auth: { status: string; user: { role: string; name: string } | null };
vi.mock("@/lib/auth/AuthProvider", () => ({ useAuth: () => auth }));

const { MyAppointments, canCheckIn } = await import("@/components/appointments/MyAppointments");
const { JoinQueue } = await import("@/components/booking/JoinQueue");

// Noon in Kolkata (the business zone) on 7 Jan 2030; the browser itself is in Los Angeles.
const NOON = Date.parse("2030-01-07T06:30:00Z");
const BUSINESS = { id: "b1", name: "Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {} };
const row = (id: string, start: string, status = "booked") => ({
  id, status, start, end: start, created_at: start, business_id: "b1", service_id: "s1", resource_id: "r1", service_name: "Checkup",
});
let ROWS: ReturnType<typeof row>[];
const START_ROWS = () => [
  row("earlier", "2030-01-07T09:00:00+05:30"), // today, already started: listed under Past
  row("later", "2030-01-07T15:00:00+05:30"), // today, upcoming
  row("tomorrow", "2030-01-08T10:00:00+05:30"), // not today: no Check in
];

type Call = { method: string; path: string; key: string | null };
let calls: Call[];
let answer: () => Response; // the next check-in or join answer
let active: { id: string; business_id: string }[];
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const TOKEN = { id: "t1", business_id: "b1", queue_id: "q1", token_no: 3, status: "waiting", source: "appointment", queued_at: "", joined_at: "", version: 1, ahead_count: 0, eta: null };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], shouldAdvanceTime: true });
  vi.setSystemTime(NOON);
  push.mockReset();
  ROWS = START_ROWS();
  calls = [];
  active = [];
  auth = { status: "signed-in", user: { role: "customer", name: "C" } };
  answer = () => json(201, TOKEN);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const method = init.method ?? "GET";
      calls.push({ method, path: url.pathname, key: new Headers(init.headers).get("Idempotency-Key") });
      if (method === "POST") {
        await new Promise((resolve) => setTimeout(resolve, 20)); // in flight long enough for a second press
        return answer();
      }
      if (url.pathname === "/v1/tokens") return json(200, active);
      if (url.pathname === "/v1/businesses/b1/queue") return json(200, { id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2030-01-07", status: "open" });
      if (url.pathname === "/v1/appointments") {
        const from = url.searchParams.get("from");
        const to = url.searchParams.get("to");
        const status = url.searchParams.get("status");
        const items = ROWS.filter((item) => (!status || item.status === status) && (!from || item.start >= from) && (!to || item.start < to));
        return json(200, { items: items.sort((a, b) => Date.parse(a.start) - Date.parse(b.start)), next_cursor: null });
      }
      return json(200, BUSINESS);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function show(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}
const posts = () => calls.filter((call) => call.method === "POST");
const checkIn = async (name: RegExp) => screen.findByRole("button", { name });

describe("Check in", () => {
  it("is offered on today's booked appointments in both lists, never on another day's", async () => {
    show(<MyAppointments />);
    expect(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/)).toBeTruthy(); // Upcoming
    expect(screen.getByRole("button", { name: /^Check in Checkup on Mon, Jan 7 at 9:00 AM/ })).toBeTruthy(); // Past
    expect(screen.queryByRole("button", { name: /^Check in Checkup on Tue, Jan 8/ })).toBeNull();
    expect(canCheckIn(ROWS[2], BUSINESS, new Date(NOON))).toBe(false);
    expect(canCheckIn({ ...ROWS[1], status: "cancelled" }, BUSINESS, new Date(NOON))).toBe(false);
  });

  it("checks in once for a double press and opens the ticket", async () => {
    show(<MyAppointments />);
    const button = await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/);
    // Both presses inside one act(): no re-render in between, so only the in-flight guard stops the second.
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/ticket/t1"));
    expect(posts()).toHaveLength(1);
    expect(posts()[0].path).toBe("/v1/appointments/later/check-in");
    expect(posts()[0].key).toMatch(/\S/);
  });

  it("a retry after a failure reuses the press's Idempotency-Key", async () => {
    answer = () => json(503, { code: "SERVICE_UNAVAILABLE" });
    show(<MyAppointments />);
    const button = await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/);
    fireEvent.click(button);
    await screen.findByRole("alert");
    answer = () => json(200, TOKEN); // the idempotent 200 of an existing check-in also opens the ticket
    fireEvent.click(button);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/ticket/t1"));
    expect(posts()).toHaveLength(2);
    expect(posts()[1].key).toBe(posts()[0].key);
  });

  it.each([
    ["too_early", "Check-in opens 30 minutes before your appointment time.", null],
    ["too_late", "Your appointment time has passed. You can still join the queue as a walk-in.", ["Join the queue at Clinic", "/b/b1"]],
    ["not_booked", "This appointment can't be checked in: it was cancelled, moved or already checked in.", null],
  ])("%s: the server's reason in words (never its text, never a clock time)", async (reason, text, link) => {
    answer = () => json(409, { code: "INVALID_TRANSITION", detail: "ignored", reason });
    show(<MyAppointments />);
    fireEvent.click(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(text);
    expect(alert.textContent).not.toContain("ignored");
    if (link) expect(within(alert).getByRole("link", { name: link[0] }).getAttribute("href")).toBe(link[1]);
    else expect(within(alert).queryByRole("link")).toBeNull();
  });

  it("not_booked refreshes the list, and the words stay after the appointment leaves it", async () => {
    answer = () => {
      ROWS[1] = { ...ROWS[1], status: "cancelled" }; // cancelled elsewhere: the refreshed list drops it
      return json(409, { code: "INVALID_TRANSITION", reason: "not_booked" });
    };
    show(<MyAppointments />);
    const button = await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/);
    const lists = () => calls.filter((call) => call.path === "/v1/appointments").length;
    const loaded = lists(); // the four lists of the first load
    fireEvent.click(button);
    await screen.findByRole("alert");
    await waitFor(() => expect(lists()).toBeGreaterThan(loaded));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^Check in Checkup on Mon, Jan 7 at 3:00 PM/ })).toBeNull());
    expect(screen.getByRole("alert").textContent).toBe(
      "This appointment can't be checked in: it was cancelled, moved or already checked in.",
    );
  });

  it("QUEUE_CLOSED has its own words", async () => {
    answer = () => json(409, { code: "QUEUE_CLOSED" });
    show(<MyAppointments />);
    fireEvent.click(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/));
    expect((await screen.findByRole("alert")).textContent).toBe("This queue isn't taking new people right now.");
  });

  it("ALREADY_IN_QUEUE links to that ticket, or to My tickets when it cannot be found", async () => {
    answer = () => json(409, { code: "ALREADY_IN_QUEUE" });
    active = [{ id: "t9", business_id: "b1" }];
    const first = show(<MyAppointments />);
    fireEvent.click(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("You already have a ticket in this queue.");
    expect(within(alert).getByRole("link", { name: "Open your ticket" }).getAttribute("href")).toBe("/ticket/t9");
    first.unmount();
    active = [];
    show(<MyAppointments />);
    fireEvent.click(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/));
    expect(within(await screen.findByRole("alert")).getByRole("link", { name: "My tickets" }).getAttribute("href")).toBe("/ticket");
  });

  it("the hourly limit says so and counts down Retry-After before Check in works again", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"], shouldAdvanceTime: true });
    vi.setSystemTime(NOON);
    answer = () => json(429, { code: "RATE_LIMITED" }, { "Retry-After": "5" });
    show(<MyAppointments />);
    fireEvent.click(await checkIn(/^Check in Checkup on Mon, Jan 7 at 3:00 PM/));
    expect((await screen.findByRole("alert")).textContent).toBe("You've joined or checked in many times this hour. Try again in 5 s.");
    const waiting = await screen.findByRole("button", { name: /^Check in \(wait 5 s\)/ });
    expect((waiting as HTMLButtonElement).disabled).toBe(true);
    await act(async () => void vi.advanceTimersByTime(5000));
    expect((screen.getByRole("button", { name: /^Check in Checkup on Mon, Jan 7 at 3:00 PM/ }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("Join the queue", () => {
  it("signed out: sign in first and come back to this business", () => {
    auth = { status: "signed-out", user: null };
    show(<JoinQueue businessId="b1" />);
    expect(screen.getByRole("link", { name: "Sign in to join the queue" }).getAttribute("href")).toBe("/login?next=%2Fb%2Fb1");
  });

  it.each(["staff", "owner", "admin"])("%s: told joining is for customer accounts, no button", (role) => {
    auth = { status: "signed-in", user: { role, name: "S" } };
    show(<JoinQueue businessId="b1" />);
    expect(screen.getByText("Joining the queue is for customer accounts.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("nothing at all until the session is known", () => {
    auth = { status: "loading", user: null };
    const { container } = show(<JoinQueue businessId="b1" />);
    expect(container.innerHTML).toBe("");
  });

  it("joins today's queue once for a double press and opens the ticket", async () => {
    show(<JoinQueue businessId="b1" />);
    const button = screen.getByRole("button", { name: "Join the queue" });
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/ticket/t1"));
    expect(posts().map((call) => call.path)).toEqual(["/v1/queues/q1/tokens"]);
  });

  it.each([
    ["QUEUE_CLOSED", "This queue isn't taking new people right now."],
    ["WALK_INS_FULL", "Walk-ins are full for today."],
  ])("%s has its own words", async (code, text) => {
    answer = () => json(409, { code });
    show(<JoinQueue businessId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: "Join the queue" }));
    expect((await screen.findByRole("alert")).textContent).toBe(text);
  });

  it("ALREADY_IN_QUEUE links to the ticket", async () => {
    answer = () => json(409, { code: "ALREADY_IN_QUEUE" });
    active = [{ id: "t4", business_id: "b1" }];
    show(<JoinQueue businessId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: "Join the queue" }));
    expect(within(await screen.findByRole("alert")).getByRole("link", { name: "Open your ticket" }).getAttribute("href")).toBe("/ticket/t4");
  });

  it("the hourly limit counts down before Join works again", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"], shouldAdvanceTime: true });
    answer = () => json(429, { code: "RATE_LIMITED" }, { "Retry-After": "5" });
    show(<JoinQueue businessId="b1" />);
    fireEvent.click(screen.getByRole("button", { name: "Join the queue" }));
    expect((await screen.findByRole("button", { name: "Join the queue (wait 5 s)" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => void vi.advanceTimersByTime(5000));
    expect((screen.getByRole("button", { name: "Join the queue" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

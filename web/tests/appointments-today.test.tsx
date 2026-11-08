import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { QueueSnapshot, SnapshotToken, StaffAppointment } from "@/lib/api/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), usePathname: () => "/console", useSearchParams: () => new URLSearchParams(window.location.search) }));

const { AppointmentsToday } = await import("@/components/console/AppointmentsToday");
const { APPOINTMENT_LABEL, CAPPED_NOTICE, MAX_PAGES, PAGE_LIMIT, POLL_MS, changedRowNotice, checkInMessage, checkedInNotice, rowAction, tokenFor } =
  await import("@/lib/console/appointments");
const { PRIMARY_HOLD_MS } = await import("@/lib/console/actions");
const { dayRange, dayStart } = await import("@/lib/time");

const ZONE = "Asia/Kolkata"; // ahead of the test browser (UTC), and never DST

const appointment = (over: Partial<StaffAppointment> = {}): StaffAppointment => ({
  id: "a1",
  business_id: "b1",
  service_id: "sv1",
  service_name: "Checkup",
  resource_id: "r1",
  start: "2026-11-02T09:30:00+05:30",
  end: "2026-11-02T10:00:00+05:30",
  status: "booked",
  created_at: "2026-11-01T09:00:00+05:30",
  customer: { id: "c1-0000-1111", name: "Ana Patel" },
  ...over,
});

const token = (over: Partial<SnapshotToken> = {}): SnapshotToken => ({
  id: "t1",
  token_no: 7,
  status: "waiting",
  priority: 0,
  source: "appointment",
  skip_count: 0,
  version: 2,
  joined_at: "2026-11-02T09:05:00+05:30",
  queued_at: "2026-11-02T09:05:00+05:30",
  called_at: null,
  started_at: null,
  order_position: 1,
  appointment_id: "a1",
  service_id: "sv1",
  service_name: "Checkup",
  customer: { id: "c1-0000-1111", name: "Ana Patel" },
  ...over,
});

const snapshotOf = (rows: SnapshotToken[]): QueueSnapshot => ({
  id: "q1",
  business_id: "b1",
  resource_id: "r1",
  queue_date: "2026-11-02",
  status: "open",
  counts: { completed: 0, no_show: 0, cancelled: 0 },
  tokens: rows,
});

type Call = { url: string; method: string; key: string | null };
let calls: Call[];
let appointments: StaffAppointment[];
let tokens: SnapshotToken[];
let answers: ((call: Call) => Response)[];
let pages: number; // how many cursor pages the fake list hands out
let mineStatus: number;
let feed: (text: string) => void;
let eventId = 500;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const call: Call = { url, method: init.method ?? "GET", key: new Headers(init.headers).get("Idempotency-Key") };
      calls.push(call);
      if (url.includes("/businesses/mine")) {
        if (mineStatus !== 200) return Response.json({ code: "NOT_FOUND" }, { status: mineStatus });
        return Response.json([{ id: "b1", name: "North Clinic", role: "staff", category: "clinic", address: null, timezone: ZONE, open_hours: {} }]);
      }
      if (url.endsWith("/queue")) return Response.json({ id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2026-11-02", status: "open" });
      if (url.includes("/queues/q1/stream-token")) return Response.json({ stream_token: "s1", expires_in: 60 });
      if (url.includes("/queues/q1/stream")) {
        const body = new ReadableStream<Uint8Array>({
          start: (controller) => {
            feed = (text: string) => controller.enqueue(new TextEncoder().encode(text));
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.includes("/queues/q1") && call.method === "GET") return Response.json(snapshotOf(tokens));
      if (url.includes("/v1/appointments?")) {
        const asked = new URL(url, "http://local").searchParams;
        const page = Number(asked.get("cursor") ?? 0);
        const more = page + 1 < pages;
        const items = page === 0 ? appointments : appointments.map((item) => ({ ...item, id: `${item.id}-p${page}` }));
        return Response.json({ items, next_cursor: more ? String(page + 1) : null });
      }
      const answer = answers.shift();
      return answer ? answer(call) : Response.json({ ...token(), id: "t1" }, { status: 201 });
    }),
  );
}

let client: QueryClient;
function show(node: ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

/** A check-in somewhere else, arriving the way it really does: a token delta whose source is
 *  "appointment". The list is what carries the appointment's new status. */
async function remoteCheckIn(next: StaffAppointment[], rows: SnapshotToken[]) {
  appointments = next;
  tokens = rows;
  eventId += 1;
  const [row] = rows;
  const envelope = { id: eventId, type: "token.joined", queue_id: "q1", data: { ...row, token_id: row.id } };
  await act(async () => {
    feed(`id: ${eventId}\nevent: token.joined\ndata: ${JSON.stringify(envelope)}\n\n`);
    await Promise.resolve();
  });
}

const rows = () => screen.getAllByRole("listitem");
const said = () => screen.getByRole("status");

beforeEach(() => {
  calls = [];
  answers = [];
  pages = 1;
  mineStatus = 200;
  appointments = [appointment()];
  tokens = [];
  serve();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// --- the rules, on their own ---

describe("appointment rules", () => {
  it("has a word for every status the database allows", () => {
    expect(Object.keys(APPOINTMENT_LABEL).sort()).toEqual(
      ["booked", "cancelled", "checked_in", "completed", "in_service", "no_show", "rescheduled"].sort(),
    );
  });

  it.each([
    ["booked", false, "check-in"],
    ["booked", true, "check-in"],
    ["checked_in", true, "open"],
    ["checked_in", false, "none"],
    ["in_service", true, "open"],
    ["in_service", false, "none"],
    ["completed", true, "none"],
    ["completed", false, "none"],
    ["cancelled", false, "none"],
    ["cancelled", true, "none"],
    ["no_show", true, "none"],
    ["rescheduled", false, "none"],
  ])("a %s row with token=%s offers %s", (status, hasToken, expected) => {
    expect(rowAction(status, hasToken as boolean)).toBe(expected);
  });

  it("finds the token that belongs to an appointment", () => {
    expect(tokenFor(appointment(), [token({ appointment_id: "other" }), token()])?.token_no).toBe(7);
    expect(tokenFor(appointment(), [token({ appointment_id: "other" })])).toBeUndefined();
  });

  it.each([
    [new ApiError(409, "INVALID_TRANSITION", null, { reason: "too_early" }), "Check-in opens 30 minutes before the appointment time."],
    [new ApiError(409, "INVALID_TRANSITION", null, { reason: "too_late" }), "That appointment time has passed. They can still join the queue as a walk-in."],
    [new ApiError(409, "INVALID_TRANSITION", null, { reason: "not_booked" }), "This appointment can't be checked in: it was cancelled, moved or already finished."],
    [new ApiError(409, "ALREADY_IN_QUEUE", null, {}), "This customer already has a ticket in today's queue."],
    [new ApiError(409, "QUEUE_CLOSED", null, {}), "This queue isn't taking check-ins right now."],
    [new ApiError(404, "NOT_FOUND", null, {}), "This appointment isn't there any more. The list is up to date now."],
    [new ApiError(403, "FORBIDDEN", null, {}), "Only this business's staff and owner can check people in."],
    [new ApiError(429, "RATE_LIMITED", 90, {}), "Too many check-ins and joins this hour on this account. Try again in 2 minutes."],
  ])("says what the server refused: %s", (error, copy) => {
    expect(checkInMessage(error)).toBe(copy);
  });

  it("503 and a network failure keep the shared words", () => {
    expect(checkInMessage(new ApiError(503, "SERVICE_UNAVAILABLE", 30, {}))).toContain("Try again in 30 s");
    expect(checkInMessage(new ApiError(0, "NETWORK", null, {}))).toContain("offline");
  });

  it("a repeat is a success, not an error", () => {
    expect(checkedInNotice("Ana Patel", 7, true)).toBe("Checked in Ana Patel, token #7");
    expect(checkedInNotice("Ana Patel", 7, false)).toBe("Already checked in, token #7");
  });

  it("says what somebody else's change did", () => {
    expect(changedRowNotice("Ana Patel", "checked_in")).toBe("Ana Patel is now checked in");
  });
});

// --- today, in the business zone ---

describe("the day in the business zone", () => {
  const noon = new Date("2026-11-02T12:00:00Z");

  it("a zone ahead of the browser starts the day before the browser's midnight", () => {
    const { from, to } = dayRange("Asia/Kolkata", noon);
    expect(from).toBe("2026-11-01T18:30:00.000Z"); // 2 Nov 00:00 +05:30
    expect(to).toBe("2026-11-02T18:30:00.000Z");
  });

  it("a zone behind the browser starts after it", () => {
    const { from, to } = dayRange("America/Los_Angeles", noon);
    expect(from).toBe("2026-11-02T08:00:00.000Z"); // 2 Nov 00:00 PST
    expect(to).toBe("2026-11-03T08:00:00.000Z");
  });

  it("the day the clocks go forward is 23 hours", () => {
    const from = dayStart("2026-03-08", "America/Los_Angeles");
    const to = dayStart("2026-03-09", "America/Los_Angeles");
    expect((to.getTime() - from.getTime()) / 3_600_000).toBe(23);
  });

  it("the day the clocks go back is 25 hours", () => {
    const from = dayStart("2026-11-01", "America/Los_Angeles");
    const to = dayStart("2026-11-02", "America/Los_Angeles");
    expect((to.getTime() - from.getTime()) / 3_600_000).toBe(25);
  });
});

// --- the page ---

describe("the appointments page", () => {
  it("lists today's appointments in the business zone, with names only", async () => {
    appointments = [
      appointment({ id: "a2", start: "2026-11-02T11:00:00+05:30", end: "2026-11-02T11:30:00+05:30", customer: { id: "c2", name: "Bo Lin" } }),
      appointment(),
    ];
    show(<AppointmentsToday businessId="b1" />);
    await screen.findByText("Ana Patel");
    const asked = calls.find((call) => call.url.includes("/v1/appointments?"))!.url;
    expect(asked).toContain("business_id=b1");
    expect(asked).toContain(`limit=${PAGE_LIMIT}`);
    // Kolkata midnight is 18:30 UTC the day before; the browser's own midnight would be 00:00Z.
    expect(asked).toMatch(/from=\d{4}-\d{2}-\d{2}T18%3A30%3A00\.000Z/);
    // 9:30 AM in Kolkata, not 4:00 AM in the browser's UTC.
    expect(within(rows()[1]).getByText("9:30 AM–10:00 AM")).toBeTruthy();
    expect(within(rows()[0]).getByText("11:00 AM–11:30 AM")).toBeTruthy();
    expect(screen.getAllByText("Checkup")).toHaveLength(2);
    expect(screen.getAllByText("Booked").length).toBe(2);
  });

  it("shows the token number once the customer is checked in, and offers the queue", async () => {
    appointments = [appointment({ status: "checked_in" })];
    tokens = [token()];
    show(<AppointmentsToday businessId="b1" />);
    expect(await screen.findByText("Token #7")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open in queue" }).getAttribute("href")).toBe("/console/b1?token=t1");
    expect(screen.queryByRole("button", { name: "Check in" })).toBeNull();
  });

  it("the queue link carries the token, and the console selects it on arrival", async () => {
    appointments = [appointment({ status: "checked_in" })];
    tokens = [token()];
    show(<AppointmentsToday businessId="b1" />);
    const open = await screen.findByRole("link", { name: "Open in queue" });
    expect(open.getAttribute("href")).toBe("/console/b1?token=t1");
    // What the console does with it, with the router reporting that link's query.
    window.history.replaceState({}, "", "/console/b1?token=t1");
    const { QueueConsole } = await import("@/components/console/QueueConsole");
    show(<QueueConsole businessId="b1" />);
    const row = await screen.findByRole("button", { name: /^#7/ });
    expect(row.getAttribute("aria-pressed")).toBe("true");
    window.history.replaceState({}, "", "/");
  });

  it("says when there is nothing today", async () => {
    appointments = [];
    show(<AppointmentsToday businessId="b1" />);
    expect(await screen.findByText("No appointments today")).toBeTruthy();
  });

  it("renders the customer's name and nothing that identifies them", async () => {
    appointments = [appointment()];
    const view = show(<AppointmentsToday businessId="b1" />);
    await screen.findByText("Ana Patel");
    const text = view.container.textContent ?? "";
    for (const secret of ["c1-0000-1111", "a1", "sv1", "r1", "@", "+91"]) {
      expect(text.includes(secret)).toBe(false);
    }
  });

  it("a cancelled appointment shows its label and offers nothing", async () => {
    appointments = [appointment({ status: "cancelled" })];
    show(<AppointmentsToday businessId="b1" />);
    expect(await screen.findByText("Cancelled")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Check in" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Open in queue" })).toBeNull();
  });

  it("checks in with one keyed request and says who got which token", async () => {
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    appointments = [appointment({ status: "checked_in" })];
    tokens = [token()];
    await act(async () => {
      fireEvent.click(press);
    });
    const sent = calls.filter((call) => call.url.includes("/check-in"));
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe("POST");
    expect(sent[0].key).toMatch(/[0-9a-f-]{36}/);
    await waitFor(() => expect(said().textContent).toBe("Checked in Ana Patel, token #7"));
  });

  it("a double press checks in once", async () => {
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    // Both clicks inside one act(): the disabled attribute has not been applied yet, so only
    // the guard in run() can stop the second one.
    await act(async () => {
      fireEvent.click(press);
      fireEvent.click(press);
    });
    expect(calls.filter((call) => call.url.includes("/check-in"))).toHaveLength(1);
  });

  it("a 200 means somebody else got there first, and is not an error", async () => {
    answers = [() => Response.json(token(), { status: 200 })];
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    await act(async () => {
      fireEvent.click(press);
    });
    await waitFor(() => expect(said().textContent).toBe("Already checked in, token #7"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    [new ApiError(409, "INVALID_TRANSITION", null, { reason: "too_early" }), "Check-in opens 30 minutes before the appointment time."],
    [new ApiError(409, "INVALID_TRANSITION", null, { reason: "too_late" }), "That appointment time has passed. They can still join the queue as a walk-in."],
    [new ApiError(409, "ALREADY_IN_QUEUE", null, {}), "This customer already has a ticket in today's queue."],
    [new ApiError(409, "QUEUE_CLOSED", null, {}), "This queue isn't taking check-ins right now."],
    [new ApiError(403, "FORBIDDEN", null, {}), "Only this business's staff and owner can check people in."],
  ])("shows the words for %s", async (error, copy) => {
    answers = [() => Response.json({ code: error.code, reason: error.body.reason }, { status: error.status })];
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    await act(async () => {
      fireEvent.click(press);
    });
    expect((await screen.findByRole("alert")).textContent).toContain(copy);
  });

  it("the rate limit says it is shared with walk-in joins, with its countdown", async () => {
    answers = [() => Response.json({ code: "RATE_LIMITED" }, { status: 429, headers: { "Retry-After": "90" } })];
    show(<AppointmentsToday businessId="b1" />);
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Check in" }));
    });
    expect((await screen.findByRole("alert")).textContent).toContain("check-ins and joins this hour");
    expect((await screen.findByRole("alert")).textContent).toContain("2 minutes");
  });

  it("after 5 s the row says so and a retry reuses the same key", async () => {
    let release: () => void = () => {};
    answers = [
      () => {
        throw new Error("unused");
      },
    ];
    answers = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        const call: Call = { url, method: init.method ?? "GET", key: new Headers(init.headers).get("Idempotency-Key") };
        calls.push(call);
        if (url.includes("/businesses/mine")) {
          return Response.json([{ id: "b1", name: "North Clinic", role: "staff", category: "clinic", address: null, timezone: ZONE, open_hours: {} }]);
        }
        if (url.endsWith("/queue")) return Response.json({ id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2026-11-02", status: "open" });
        if (url.includes("/queues/q1/stream-token")) return Response.json({ stream_token: "s1", expires_in: 60 });
        if (url.includes("/queues/q1/stream")) return new Response(new ReadableStream(), { status: 200, headers: { "Content-Type": "text/event-stream" } });
        if (url.includes("/queues/q1")) return Response.json(snapshotOf(tokens));
        if (url.includes("/v1/appointments?")) return Response.json({ items: appointments, next_cursor: null });
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return Response.json(token(), { status: 201 });
      }),
    );
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    await act(async () => {
      fireEvent.click(press);
    });
    expect(await screen.findByText("Still working… check the list.", {}, { timeout: 8000 })).toBeTruthy();
    const first = calls.filter((call) => call.url.includes("/check-in"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check in" }));
    });
    const sent = calls.filter((call) => call.url.includes("/check-in"));
    expect(sent).toHaveLength(2);
    expect(sent[1].key).toBe(first[0].key); // the same deliberate press, retried
    act(() => release());
  }, 20_000);

  it("a new deliberate press on another row gets its own key", async () => {
    appointments = [appointment(), appointment({ id: "a2", customer: { id: "c2", name: "Bo Lin" } })];
    show(<AppointmentsToday businessId="b1" />);
    await screen.findByText("Ana Patel");
    const buttons = screen.getAllByRole("button", { name: "Check in" });
    await act(async () => {
      fireEvent.click(buttons[0]);
    });
    await act(async () => {
      fireEvent.click(buttons[1]);
    });
    const sent = calls.filter((call) => call.url.includes("/check-in"));
    expect(sent).toHaveLength(2);
    expect(sent[0].key).not.toBe(sent[1].key);
  });

  it("someone else's check-in relabels the row, holds it, and a click inside sends nothing", async () => {
    show(<AppointmentsToday businessId="b1" />);
    await screen.findByRole("button", { name: "Check in" });
    await remoteCheckIn([appointment({ status: "checked_in" })], [token()]);
    // The delta triggers a read of the list once the burst has settled.
    await waitFor(() => expect(screen.getByText("Token #7")).toBeTruthy(), { timeout: 6000 });
    await waitFor(() => expect(said().textContent).toBe("Ana Patel is now checked in"));
    expect(screen.queryByRole("button", { name: "Check in" })).toBeNull();
    // Inside the hold the row shows the new action as a dead button: a click already on its
    // way cannot open the queue either.
    const heldAction = screen.getByRole("button", { name: "Open in queue" });
    expect(heldAction.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("link", { name: "Open in queue" })).toBeNull();
    fireEvent.click(heldAction);
    expect(calls.filter((call) => call.url.includes("/check-in"))).toHaveLength(0);
    // Once the hold is over the link is there.
    await waitFor(() => expect(screen.getByRole("link", { name: "Open in queue" })).toBeTruthy(), { timeout: 4000 });
  }, 20_000);

  it("the first load never holds a row", async () => {
    appointments = [appointment({ status: "checked_in" })];
    tokens = [token()];
    show(<AppointmentsToday businessId="b1" />);
    const open = await screen.findByRole("link", { name: "Open in queue" });
    expect(open.getAttribute("aria-disabled")).toBeNull();
    expect(said().textContent).toBe("");
  });

  it("the user's own check-in never holds the row", async () => {
    show(<AppointmentsToday businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Check in" });
    appointments = [appointment({ status: "checked_in" })];
    tokens = [token()];
    await act(async () => {
      fireEvent.click(press);
    });
    await waitFor(() => expect(said().textContent).toBe("Checked in Ana Patel, token #7"));
    // Not "Ana Patel is now checked in": the change is the answer to their own press.
    await new Promise((resolve) => setTimeout(resolve, PRIMARY_HOLD_MS + 50));
    expect(said().textContent).toBe("Checked in Ana Patel, token #7");
  }, 20_000);

  it("reads the list again when a delta from an appointment arrives", async () => {
    show(<AppointmentsToday businessId="b1" />);
    await screen.findByText("Ana Patel");
    const before = calls.filter((call) => call.url.includes("/v1/appointments?")).length;
    await remoteCheckIn([appointment({ status: "checked_in" })], [token()]);
    await waitFor(() => expect(calls.filter((call) => call.url.includes("/v1/appointments?")).length).toBeGreaterThan(before), { timeout: 6000 });
  }, 20_000);

  it("follows every page, and stops after ten against a server that never runs out", async () => {
    pages = Number.MAX_SAFE_INTEGER; // a cursor on every page, for ever
    show(<AppointmentsToday businessId="b1" />);
    expect(await screen.findByText(CAPPED_NOTICE)).toBeTruthy();
    // Ten, written out: a cap that reads itself from the constant could not catch it moving.
    expect(calls.filter((call) => call.url.includes("/v1/appointments?"))).toHaveLength(10);
    expect(MAX_PAGES).toBe(10);
  });

  it("polls on a slow tick, and not while the tab is hidden", async () => {
    // Fake timers from the start, never installed mid-test: a hook parked on a real timer
    // would never move again (the 6D-2 lesson). No waitFor here either: it runs its own clock.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    const view = show(<AppointmentsToday businessId="b1" />);
    const reads = () => calls.filter((call) => call.url.includes("/v1/appointments?")).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(reads()).toBeGreaterThan(0);
    const before = reads();
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(reads()).toBe(before); // nobody is reading the page
    hidden.mockReturnValue(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(reads()).toBeGreaterThan(before);
    hidden.mockRestore();
    view.unmount();
    vi.useRealTimers();
  }, 20_000);

  it("the access-ended and not-yours words are the console's own", async () => {
    mineStatus = 404;
    show(<AppointmentsToday businessId="b1" />);
    expect(await screen.findByRole("heading", { name: "Your access to this business has ended" })).toBeTruthy();
  });

  it("a business that was never ours reads neutrally", async () => {
    show(<AppointmentsToday businessId="someone-elses" />);
    expect(await screen.findByRole("heading", { name: "You don't have access to this business." })).toBeTruthy();
  });

  it("the poll interval is the named 30 s", () => {
    expect(POLL_MS).toBe(30_000);
  });
});

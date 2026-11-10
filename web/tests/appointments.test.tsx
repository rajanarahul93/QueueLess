import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/auth/AuthProvider", () => ({
  useAuth: () => ({ status: "signed-in", user: { role: "customer", name: "C" } }),
}));

const { MyAppointments } = await import("@/components/appointments/MyAppointments");
const { Reschedule, RescheduleTitle } = await import("@/components/appointments/Reschedule");

// jsdom has <dialog> but not showModal/close.
HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

const SERVER_NOW = Date.now(); // the server's clock; the browser's may be faked below
const HOUR = 3_600_000;
const at = (ms: number) => new Date(ms).toISOString();
const ZONE = "Asia/Kolkata";

type Row = { id: string; status: string; start: string };
let rows: Row[];
let overflow: boolean;
let calls: { method: string; url: string; key: string | null }[];
let onPatch: () => Response;

function json(status: number, body: unknown) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const item = (row: Row) => ({
  ...row,
  business_id: "b1",
  service_id: "s1",
  resource_id: "r1",
  service_name: "Checkup",
  end: row.start,
  created_at: row.start,
});

/** Filters like the API: one status, start >= from, start < to, ascending, 50 a page. */
function list(url: URL) {
  const status = url.searchParams.get("status");
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  const items = rows
    .filter((row) => (!status || row.status === status) && (!from || row.start >= from) && (!to || row.start < to))
    .sort((a, b) => a.start.localeCompare(b.start))
    .map(item);
  return { items, next_cursor: overflow ? "next" : null };
}

beforeEach(() => {
  overflow = false;
  calls = [];
  onPatch = () => json(200, { ...item({ id: "new", status: "booked", start: "2030-01-07T10:00:00+05:30" }), rescheduled_from_id: "up" });
  rows = [
    { id: "up", status: "booked", start: "2030-01-07T09:00:00+05:30" }, // 7:30 PM the day before in LA
    { id: "older", status: "completed", start: at(SERVER_NOW - 48 * HOUR) },
    { id: "newer", status: "cancelled", start: at(SERVER_NOW - 24 * HOUR) },
    { id: "moved", status: "rescheduled", start: at(SERVER_NOW + 48 * HOUR) },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const method = init.method ?? "GET";
      calls.push({ method, url: input, key: new Headers(init.headers).get("Idempotency-Key") });
      if (url.pathname === "/v1/appointments") return json(200, list(url));
      if (method === "DELETE") {
        await new Promise((resolve) => setTimeout(resolve, 30)); // in flight long enough for a second click
        const row = rows.find((candidate) => url.pathname.endsWith(candidate.id));
        if (row?.status === "cancelled") return json(204, null);
        if (!row || row.status !== "booked" || Date.parse(row.start) <= SERVER_NOW) {
          return json(409, { code: "INVALID_TRANSITION" }); // the server decides, on its own clock
        }
        row.status = "cancelled";
        return json(204, null);
      }
      if (method === "PATCH") return onPatch();
      if (url.pathname.endsWith("/availability")) {
        const date = url.searchParams.get("date");
        return json(200, {
          timezone: ZONE,
          slots: ["09:30", "10:00"].map((time) => ({ start: `${date}T${time}:00+05:30`, end: "", resource_id: "r1" })),
        });
      }
      if (url.pathname.endsWith("/services")) {
        return json(200, [{ id: "s1", name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00", is_active: true }]);
      }
      return json(200, { id: "b1", name: "Clinic", category: "clinic", address: null, timezone: ZONE, open_hours: {} });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function renderWith(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const section = (name: string) => screen.getByRole("heading", { name }).nextElementSibling as HTMLElement;
const deletes = () => calls.filter((call) => call.method === "DELETE");

describe("My appointments", () => {
  it("lists upcoming in the business zone and the past newest first, last 30 days", async () => {
    renderWith(<MyAppointments />);
    expect(await screen.findByText("Checkup on Mon, Jan 7 at 9:00 AM", { selector: "p" })).toBeTruthy();
    expect(screen.getByText("Showing the last 30 days.")).toBeTruthy();
    expect(screen.queryByText(/more than 50/)).toBeNull();
    const past = screen.getAllByText(/Clinic · (Completed|Cancelled)/).map((line) => line.textContent);
    expect(past).toEqual(["Clinic · Cancelled", "Clinic · Completed"]); // newest first
    expect(screen.queryByText(/Moved/)).toBeNull(); // future but not booked: in neither list
    const upcomingQuery = calls.find((call) => call.url.includes("status=booked"))!.url;
    expect(new URL(upcomingQuery).searchParams.get("limit")).toBe("50");
  });

  it("says so when the last 30 days hold more than 50, never a silent cut-off", async () => {
    overflow = true;
    renderWith(<MyAppointments />);
    expect(
      await screen.findByText("You have more than 50 appointments in the last 30 days. Only the earliest 50 are shown here."),
    ).toBeTruthy();
  });

  it("points a checked-in or in-service appointment to the live ticket", async () => {
    rows.push({ id: "in", status: "checked_in", start: at(SERVER_NOW + 0.2 * HOUR) });
    rows.push({ id: "serving", status: "in_service", start: at(SERVER_NOW - 0.5 * HOUR) });
    renderWith(<MyAppointments />);
    expect((await screen.findByRole("link", { name: "Checked in: see your live ticket" })).getAttribute("href")).toBe("/ticket");
    expect(screen.getByRole("link", { name: "In service: see your live ticket" })).toBeTruthy();
    // one request per status: the API filters a single status
    expect(calls.filter((call) => call.url.includes("/v1/appointments?")).length).toBe(4);
  });

  it("Keep it and Esc keep the appointment; focus goes back to its Cancel button", async () => {
    renderWith(<MyAppointments />);
    const cancel = await screen.findByRole("button", { name: "Cancel Checkup on Mon, Jan 7 at 9:00 AM" });
    await userEvent.click(cancel);
    const dialog = screen.getByRole("dialog", { name: "Cancel this appointment?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(document.activeElement).toBe(cancel);
    await userEvent.click(cancel);
    fireEvent(dialog, new Event("cancel")); // Esc: the browser closes it, nothing else happens
    (dialog as HTMLDialogElement).close();
    expect(deletes()).toHaveLength(0);
  });

  it("a close event that arrives after the notice is focused leaves it focused", async () => {
    renderWith(<MyAppointments />);
    const cancel = await screen.findByRole("button", { name: "Cancel Checkup on Mon, Jan 7 at 9:00 AM" });
    // Hold the list refetch, so the Cancel button is still on the page when the close event
    // arrives: in the browser the refetch removed it 6 ms after the restore had focused it.
    const stubbed = globalThis.fetch as typeof fetch;
    vi.stubGlobal("fetch", (async (input: string, init?: RequestInit) =>
      String(input).includes("/v1/appointments?") ? new Promise<Response>(() => {}) : stubbed(input, init)) as unknown as typeof fetch);
    await userEvent.click(cancel);
    const dialog = screen.getByRole("dialog", { name: "Cancel this appointment?" });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel appointment" }));
    });
    const done = await screen.findByRole("status");
    expect(document.activeElement).toBe(done);
    expect(cancel.isConnected).toBe(true); // the invoker the restore would jump to is still there
    // A real browser dispatches `close` as its own task, so the handler can run AFTER React has
    // committed the notice and focused it (seen in CI: the restore landed 4 ms later and the
    // refetch then removed the button it had focused, leaving focus on the body).
    await act(async () => {
      dialog.dispatchEvent(new Event("close"));
    });
    expect(document.activeElement).toBe(done);
  });

  it("a close event with focus nowhere still returns it to the opener", async () => {
    renderWith(<MyAppointments />);
    const cancel = await screen.findByRole("button", { name: "Cancel Checkup on Mon, Jan 7 at 9:00 AM" });
    await userEvent.click(cancel);
    const dialog = screen.getByRole("dialog", { name: "Cancel this appointment?" });
    (document.activeElement as HTMLElement | null)?.blur(); // focus parked on the body
    expect(document.activeElement).toBe(document.body);
    await act(async () => {
      dialog.dispatchEvent(new Event("close"));
    });
    expect(document.activeElement).toBe(cancel);
  });

  it("a double click on Cancel appointment sends ONE request, then says what was cancelled", async () => {
    renderWith(<MyAppointments />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel Checkup on Mon, Jan 7 at 9:00 AM" }));
    const confirm = screen.getByRole("button", { name: "Cancel appointment" });
    // Both clicks inside one act(): React cannot re-render (and disable the busy button) in
    // between, so only the in-flight guard stops the second request.
    await act(async () => {
      fireEvent.click(confirm);
      fireEvent.click(confirm);
    });
    const done = await screen.findByRole("status");
    expect(done.textContent).toBe("Cancelled Checkup on Mon, Jan 7 at 9:00 AM.");
    expect(document.activeElement).toBe(done);
    expect(deletes()).toHaveLength(1);
    expect(deletes()[0].key).toBeNull(); // DELETE carries no Idempotency-Key
    await waitFor(() => expect(within(section("Upcoming")).queryByText(/Jan 7/)).toBeNull()); // list refetched
  });

  it("a cancel the server refuses shows the copy and refreshes the list", async () => {
    renderWith(<MyAppointments />);
    await userEvent.click(await screen.findByRole("button", { name: /^Cancel Checkup/ }));
    rows[0].status = "checked_in"; // checked in at the desk meanwhile
    await userEvent.click(screen.getByRole("button", { name: "Cancel appointment" }));
    expect((await screen.findByRole("alert")).textContent).toBe("This appointment can't be changed any more.");
    await waitFor(() => expect(screen.getByText("No upcoming appointments.")).toBeTruthy());
  });
});

describe("Move an appointment", () => {
  async function neutral(id: string) {
    const view = renderWith(
      <>
        <RescheduleTitle id={id} />
        <Reschedule id={id} />
      </>,
    );
    await screen.findByRole("heading", { name: "This appointment can't be moved" });
    const result = { html: view.container.innerHTML, title: document.title };
    view.unmount();
    return result;
  }

  it("one neutral page and title for a wrong id, someone else's, a cancelled or a moved one", async () => {
    const wrong = await neutral("no-such-id");
    expect(wrong.title).toBe("Move appointment · QueueLess");
    rows.push({ id: "gone", status: "cancelled", start: at(SERVER_NOW + 72 * HOUR) });
    // someone else's id: the API never lists it for this customer, exactly like a wrong id
    for (const id of ["someone-elses", "gone", "moved"]) expect(await neutral(id)).toEqual(wrong);
  });

  it("names the business once found, keeps one key per chosen start, and stays after the move", async () => {
    let fail = true;
    onPatch = () => {
      if (fail) return json(503, { code: "SERVICE_UNAVAILABLE" });
      return json(200, { ...item({ id: "new", status: "booked", start: "2030-01-07T10:00:00+05:30" }), rescheduled_from_id: "up" });
    };
    renderWith(
      <>
        <RescheduleTitle id="up" />
        <Reschedule id="up" />
      </>,
    );
    expect(await screen.findByRole("heading", { name: "Move appointment at Clinic" })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe("Move appointment at Clinic · QueueLess"));
    expect(screen.getByText("Now: Checkup on Mon, Jan 7 at 9:00 AM")).toBeTruthy();
    expect(screen.queryByLabelText("Service")).toBeNull(); // the service cannot change

    await userEvent.click(await screen.findByRole("radio", { name: "9:30 AM" }));
    await userEvent.click(screen.getByRole("button", { name: "Move appointment" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Move appointment" })); // retry: same key
    await userEvent.click(screen.getByRole("radio", { name: "10:00 AM" }));
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Move appointment" })); // new start: new key
    expect(await screen.findByRole("heading", { name: "Moved" })).toBeTruthy();

    const patches = calls.filter((call) => call.method === "PATCH");
    expect(patches.map((call) => call.url.endsWith("/v1/appointments/up/reschedule"))).toEqual([true, true, true]);
    expect(patches[0].key).toBe(patches[1].key);
    expect(patches[2].key).not.toBe(patches[0].key);
    rows[0].status = "rescheduled"; // the refetched list no longer has it: the page stays
    await waitFor(() => expect(screen.getByRole("heading", { name: "Moved" })).toBeTruthy());
  });

  it("SLOT_TAKEN on a move offers the next times", async () => {
    onPatch = () =>
      json(409, { code: "SLOT_TAKEN", next_slots: [{ start: "2030-01-07T11:00:00+05:30", end: "", resource_id: "r1" }] });
    renderWith(<Reschedule id="up" />);
    await userEvent.click(await screen.findByRole("radio", { name: "9:30 AM" }));
    await userEvent.click(screen.getByRole("button", { name: "Move appointment" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("That time was just taken. Pick another. Your current appointment hasn't changed.");
    expect(within(alert).getByRole("button", { name: "11:00 AM" })).toBeTruthy();
  });
});

describe("a browser clock two hours off the server's", () => {
  beforeEach(() => {
    rows.push({ id: "soon", status: "booked", start: at(SERVER_NOW + HOUR) }); // in an hour
    rows.push({ id: "started", status: "booked", start: at(SERVER_NOW - HOUR) }); // an hour ago
  });

  it("two hours fast: an appointment within the next two hours is listed under Past; cancel still works", async () => {
    vi.useFakeTimers({ toFake: ["Date"], shouldAdvanceTime: true });
    vi.setSystemTime(SERVER_NOW + 2 * HOUR);
    renderWith(<MyAppointments />);
    await screen.findByText("Checkup on Mon, Jan 7 at 9:00 AM", { selector: "p" });
    expect(within(section("Upcoming")).queryAllByRole("listitem")).toHaveLength(1);
    expect(screen.getAllByText("Clinic · Not checked in")).toHaveLength(2); // "soon" and "started"
    await userEvent.click(screen.getByRole("button", { name: /^Cancel Checkup on Mon, Jan 7/ }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel appointment" }));
    expect((await screen.findByRole("status")).textContent).toMatch(/^Cancelled/);
  });

  it("two hours slow: one that started an hour ago is listed as upcoming; the server refuses its cancel", async () => {
    vi.useFakeTimers({ toFake: ["Date"], shouldAdvanceTime: true });
    vi.setSystemTime(SERVER_NOW - 2 * HOUR);
    renderWith(<MyAppointments />);
    await screen.findByText("Checkup on Mon, Jan 7 at 9:00 AM", { selector: "p" });
    expect(within(section("Upcoming")).getAllByRole("listitem")).toHaveLength(3);
    await userEvent.click(screen.getAllByRole("button", { name: /^Cancel Checkup/ })[0]); // "started"
    await userEvent.click(screen.getByRole("button", { name: "Cancel appointment" }));
    expect((await screen.findByRole("alert")).textContent).toBe("This appointment can't be changed any more.");
    await waitFor(() => expect(within(section("Upcoming")).getAllByRole("listitem")).toHaveLength(3));
  });
});

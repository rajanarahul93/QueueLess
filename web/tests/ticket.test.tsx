import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveStatus, Ticket } from "@/lib/stream/useLiveTicket";

// The screen against a stand-in for the stream hook: each test sets the ticket and connection.
let state: { ticket: Ticket | undefined; status: LiveStatus };
const retry = vi.fn();
vi.mock("@/lib/stream/useLiveTicket", async (original) => ({
  ...(await original<typeof import("@/lib/stream/useLiveTicket")>()),
  useLiveTicket: () => ({ ...state, retry }),
}));

const { LiveTicket, etaLabel, ANNOUNCE_EVERY_MS } = await import("@/components/ticket/LiveTicket");

HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

const WAITING: Ticket = {
  id: "t1",
  business_id: "b1",
  token_no: 7,
  status: "waiting",
  ahead_count: 2,
  next: false,
  eta: { min_minutes: 12, max_minutes: 18 },
  joined_at: "2026-11-02T09:05:00+05:30",
  late: null,
  updatedAt: Date.now(),
};

let deletes: number;
beforeEach(() => {
  deletes = 0;
  state = { ticket: { ...WAITING, updatedAt: Date.now() }, status: "live" };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit = {}) => {
      if (init.method === "DELETE") {
        deletes += 1;
        await new Promise((resolve) => setTimeout(resolve, 30));
        return new Response(null, { status: 204 });
      }
      const body = { id: "b1", name: "Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {} };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <LiveTicket id="t1" />
    </QueryClientProvider>,
  );
  const update = (next: Partial<typeof state> & { ticket?: Ticket }) => {
    state = { ...state, ...next };
    view.rerender(
      <QueryClientProvider client={client}>
        <LiveTicket id="t1" />
      </QueryClientProvider>,
    );
  };
  return { ...view, update };
}
const polite = () => document.querySelector('[aria-live="polite"]')!;
const assertive = () => document.querySelector('[aria-live="assertive"]')!;

describe("the live ticket", () => {
  it("ETA is always a range, or Calculating…, never one number", () => {
    expect(etaLabel({ min_minutes: 12, max_minutes: 18 })).toBe("12–18 min");
    expect(etaLabel(null)).toBe("Calculating…");
  });

  it("waiting: position = tokens ahead + 1, the range, the number and badge, in the business zone", async () => {
    show();
    expect(screen.getByText("Your position").nextElementSibling?.textContent).toBe("3");
    expect(screen.getByText("12–18 min")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("#7");
    expect(screen.getByText("Waiting")).toBeTruthy();
    expect(await screen.findByText("Joined at 9:05 AM (Asia/Kolkata)")).toBeTruthy(); // a Los Angeles browser
    expect(screen.getByText("Updated just now")).toBeTruthy();
  });

  it("You're next, and Calculating… without an estimate", () => {
    state.ticket = { ...WAITING, ahead_count: 0, next: true, eta: null };
    show();
    expect(screen.getByText("You're next")).toBeTruthy();
    expect(screen.getByText("Calculating…")).toBeTruthy();
  });

  it("not live: the numbers stay, the caption turns 'May be out of date'; Updated N min ago after 30 s ticks", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout", "Date"] });
    state.ticket = { ...WAITING, updatedAt: Date.now() };
    const { update } = show();
    update({ status: "reconnecting" });
    expect(screen.getByText("May be out of date · Updated just now")).toBeTruthy();
    expect(screen.getByText("Reconnecting…")).toBeTruthy();
    act(() => void vi.advanceTimersByTime(150_000));
    expect(screen.getByText("May be out of date · Updated 2 min ago")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("the pill: Live for 3 s then a dot; Live (slow); Offline with Retry now; Too many open screens", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const { update } = show();
    expect(screen.getByText("Live")).toBeTruthy();
    act(() => void vi.advanceTimersByTime(3000));
    expect(screen.getByRole("img", { name: "Live" })).toBeTruthy();
    update({ status: "polling" });
    expect(screen.getByText("Live (slow)")).toBeTruthy();
    update({ status: "offline" });
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(retry).toHaveBeenCalled();
    update({ status: "capped" });
    expect(screen.getByText(/Too many open screens/)).toBeTruthy();
    expect(screen.getByText("Live (slow)")).toBeTruthy();
  });

  it("called: the turn heading takes focus and only the assertive region announces it", () => {
    const { update } = show();
    update({ ticket: { ...WAITING, status: "called", ahead_count: null, next: true, eta: null } });
    const heading = screen.getByRole("heading", { name: "It's your turn" });
    expect(document.activeElement).toBe(heading);
    expect(assertive().textContent).toBe("It's your turn — please go to the counter.");
    expect(polite().textContent).toBe("");
    expect(screen.getByText("Called")).toBeTruthy();
  });

  it.each([
    ["completed", "You've been served. Thanks for visiting."],
    ["no_show", "You were marked as not here when called."],
    ["cancelled", "This ticket is cancelled."],
  ])("%s: an ended message with focus and a way back to the business", (status, text) => {
    const { update } = show();
    update({ ticket: { ...WAITING, status, ahead_count: null, eta: null }, status: "ended" });
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: text }));
    expect(screen.getByRole("link", { name: "Back to business" }).getAttribute("href")).toBe("/b/b1");
    expect(screen.queryByRole("button", { name: "Leave the queue" })).toBeNull();
  });

  it("in service says so, and cannot be left", () => {
    state.ticket = { ...WAITING, status: "in_service", ahead_count: null, eta: null };
    show();
    expect(screen.getByText("You're being served.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Leave the queue" })).toBeNull();
  });

  it("running late: the delay and the new range, dismissible", async () => {
    state.ticket = { ...WAITING, late: { delay_min: 15, eta: { min_minutes: 20, max_minutes: 30 } } };
    show();
    expect(screen.getByRole("status").textContent).toContain("Running about 15 min behind. Your new estimate is 20–30 min.");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/Running about/)).toBeNull();
  });

  it("position changes: announced politely, at most every 20 s and always the latest; You're next at once", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const { update } = show();
    expect(polite().textContent).toBe(""); // the first value is on the page, not announced
    update({ ticket: { ...WAITING, ahead_count: 1 } });
    act(() => void vi.advanceTimersByTime(0));
    expect(polite().textContent).toBe("Your position: 2");
    update({ ticket: { ...WAITING, ahead_count: 5 } });
    update({ ticket: { ...WAITING, ahead_count: 4 } });
    act(() => void vi.advanceTimersByTime(ANNOUNCE_EVERY_MS - 1));
    expect(polite().textContent).toBe("Your position: 2");
    act(() => void vi.advanceTimersByTime(1));
    expect(polite().textContent).toBe("Your position: 5"); // the latest value, once
    update({ ticket: { ...WAITING, ahead_count: 0, next: true } });
    act(() => void vi.advanceTimersByTime(0));
    expect(polite().textContent).toBe("You're next");
  });

  it("leave: Esc keeps the ticket; a double press leaves once", async () => {
    show();
    const leave = screen.getByRole("button", { name: "Leave the queue" });
    await userEvent.click(leave);
    const dialog = screen.getByRole("dialog", { name: "Leave the queue?" });
    fireEvent(dialog, new Event("cancel"));
    (dialog as HTMLDialogElement).close();
    expect(document.activeElement).toBe(leave);
    expect(deletes).toBe(0);
    await userEvent.click(leave);
    const confirm = within(dialog).getByRole("button", { name: "Yes, leave" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await act(() => new Promise((resolve) => setTimeout(resolve, 60)));
    expect(deletes).toBe(1);
  });
});

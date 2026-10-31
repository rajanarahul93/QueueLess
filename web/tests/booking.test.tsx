import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { addDays, dayButtonName, localToday } from "@/lib/time";

const replace = vi.fn();
const ZONE = "Asia/Kolkata";
const DATE = addDays(localToday(ZONE), 3);
let search = new URLSearchParams(`service=s1&date=${DATE}`);
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => search,
}));

const { BookingFlow, book } = await import("@/components/booking/BookingFlow");

const at = (time: string) => `${DATE}T${time}:00+05:30`;
const slot = (time: string) => ({ start: at(time), end: at(time), resource_id: "r1" });
const SLOTS = [slot("09:00"), slot("09:30"), slot("10:00")];

type Call = { method: string; url: string; key: string | null; body: unknown };
let calls: Call[];
let onPost: (call: Call) => Response;

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

beforeEach(() => {
  calls = [];
  replace.mockReset();
  search = new URLSearchParams(`service=s1&date=${DATE}`);
  onPost = () => json(201, { id: "a1", service_name: "Checkup", start: at("09:00"), end: at("09:30") });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const call = {
        method: init.method ?? "GET",
        url,
        key: new Headers(init.headers).get("Idempotency-Key"),
        body: init.body ? JSON.parse(String(init.body)) : null,
      };
      calls.push(call);
      if (call.method === "POST") return onPost(call);
      if (url.includes("/availability")) return json(200, { timezone: ZONE, slots: SLOTS });
      if (url.endsWith("/services")) {
        return json(200, [{ id: "s1", name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00", is_active: true }]);
      }
      return json(200, { id: "b1", name: "Clinic", category: "clinic", address: null, timezone: ZONE, open_hours: {} });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderFlow() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BookingFlow businessId="b1" />
    </QueryClientProvider>,
  );
}

const posts = () => calls.filter((call) => call.method === "POST");

describe("BookingFlow", () => {
  it("shows the times in the business zone even in a Los Angeles browser", async () => {
    renderFlow();
    expect(await screen.findByRole("radio", { name: "9:00 AM" })).toBeTruthy();
    expect(screen.getByText(/times shown in Asia\/Kolkata/)).toBeTruthy();
  });

  it("sends ONE request for a double click, and for Enter followed by a click", async () => {
    renderFlow();
    await userEvent.click(await screen.findByRole("radio", { name: "9:00 AM" }));
    const confirm = screen.getByRole("button", { name: "Confirm booking" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await screen.findByRole("heading", { name: "You're booked" });
    expect(posts()).toHaveLength(1);
  });

  it("Enter then click also sends one request", async () => {
    renderFlow();
    await userEvent.click(await screen.findByRole("radio", { name: "9:30 AM" }));
    const confirm = screen.getByRole("button", { name: "Confirm booking" });
    confirm.focus();
    await act(async () => {
      fireEvent.keyDown(confirm, { key: "Enter" });
      fireEvent.click(confirm); // Enter on a button clicks it; then the user clicks too
      fireEvent.click(confirm);
    });
    await screen.findByRole("heading", { name: "You're booked" });
    expect(posts()).toHaveLength(1);
  });

  it("on SLOT_TAKEN keeps the service and date, offers next_slots, and books one", async () => {
    let first = true;
    onPost = (call) => {
      if (first) {
        first = false;
        return json(409, { code: "SLOT_TAKEN", next_slots: [slot("10:00"), slot("10:30")] });
      }
      return json(201, { id: "a2", service_name: "Checkup", start: (call.body as { start: string }).start, end: at("11:00") });
    };
    renderFlow();
    await userEvent.click(await screen.findByRole("radio", { name: "9:00 AM" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm booking" }));
    const alert = await screen.findByText("That time was just taken. Pick another.");
    expect(alert.closest("[role=alert]")).toBe(document.activeElement);
    expect(replace).not.toHaveBeenCalled(); // service and date untouched
    expect(screen.getByRole("button", { name: dayButtonName(DATE), pressed: true })).toBeTruthy();
    await waitFor(() => expect(calls.filter((call) => call.url.includes("/availability")).length).toBeGreaterThan(1));
    await userEvent.click(screen.getByRole("button", { name: "10:30 AM" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm booking" }));
    await screen.findByRole("heading", { name: "You're booked" });
    const [taken, retried] = posts();
    expect(taken.body).toMatchObject({ service_id: "s1", start: at("09:00") });
    expect(retried.body).toMatchObject({ service_id: "s1", start: at("10:30") });
    expect(retried.key).not.toBe(taken.key); // another slot is another action
  });

  it("shows the limit and restriction messages where Confirm is", async () => {
    for (const [code, text] of [
      ["BOOKING_LIMIT_REACHED", "You already have 5 upcoming appointments. Cancel one to book another."],
      ["BOOKING_RESTRICTED", "Booking is paused on your account after missed appointments."],
    ]) {
      onPost = () => json(code === "BOOKING_RESTRICTED" ? 403 : 409, { code });
      const view = renderFlow();
      await userEvent.click(await screen.findByRole("radio", { name: "9:00 AM" }));
      await userEvent.click(screen.getByRole("button", { name: "Confirm booking" }));
      expect((await screen.findByRole("alert")).textContent).toContain(text);
      view.unmount();
    }
  });

  it("waits out Retry-After on RATE_LIMITED before Confirm works again", async () => {
    onPost = () => json(429, { code: "RATE_LIMITED" }, { "Retry-After": "30" });
    renderFlow();
    await userEvent.click(await screen.findByRole("radio", { name: "9:00 AM" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(await screen.findByRole("button", { name: /Confirm booking \(wait \d+ s\)/ })).toHaveProperty("disabled", true);
  });
});

describe("book()", () => {
  it("retries a duplicate still in progress once, with the SAME key", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let count = 0;
    onPost = () => {
      count += 1;
      return count === 1
        ? json(409, { code: "IDEMPOTENCY_CONFLICT", in_progress: true }, { "Retry-After": "1" })
        : json(201, { id: "a3" });
    };
    const done = book({ service_id: "s1", resource_id: "r1", start: at("09:00") }, "key-7");
    await vi.advanceTimersByTimeAsync(1000);
    expect(await done).toMatchObject({ id: "a3" });
    expect(posts().map((call) => call.key)).toEqual(["key-7", "key-7"]);
    vi.useRealTimers();
  });

  it("does not retry a conflicting body (same key, different request)", async () => {
    onPost = () => json(409, { code: "IDEMPOTENCY_CONFLICT" });
    await expect(book({ service_id: "s1", resource_id: "r1", start: at("09:00") }, "key-8")).rejects.toMatchObject({
      code: "IDEMPOTENCY_CONFLICT",
    });
    expect(posts()).toHaveLength(1);
  });
});

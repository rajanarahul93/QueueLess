import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SnapshotToken } from "@/lib/api/types";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }), usePathname: () => "/console", useSearchParams: () => new URLSearchParams(window.location.search) }));

const { consoleOrder, QueueRow } = await import("@/components/console/QueueRows");
const { StatusMenu, SLOW_PRESS_MS } = await import("@/components/console/StatusMenu");
const { BusinessPicker } = await import("@/components/console/BusinessPicker");
const { rememberBusiness, rememberedBusiness, forgetBusiness } = await import("@/lib/console/remember");
const { QueueConsole } = await import("@/components/console/QueueConsole");

HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

const ZONE = "Asia/Kolkata"; // the browser is Los Angeles (tests/setup.ts)

const row = (over: Partial<SnapshotToken> = {}): SnapshotToken => ({
  id: "t1",
  token_no: 1,
  status: "waiting",
  priority: 0,
  source: "walk_in",
  skip_count: 0,
  version: 1,
  joined_at: "2026-11-02T09:05:00+05:30",
  queued_at: "2026-11-02T09:05:00+05:30",
  called_at: null,
  started_at: null,
  order_position: 1,
  appointment_id: null,
  service_id: null,
  service_name: null,
  customer: { id: "c1", name: "Asha Customer" },
  ...over,
});

function show(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

let calls: { url: string; method: string; key: string | null; body: unknown }[];
let answer: () => Response;

beforeEach(() => {
  replace.mockReset();
  forgetBusiness();
  calls = [];
  answer = () => new Response(null, { status: 204 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({
        url,
        method: init.method ?? "GET",
        key: new Headers(init.headers).get("Idempotency-Key"),
        body: init.body ? JSON.parse(String(init.body)) : null,
      });
      return answer();
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the console list order", () => {
  it.each([
    [
      "called and in-service first, then waiting by call order",
      [row({ id: "w2", order_position: 2 }), row({ id: "s1", status: "in_service", order_position: null }), row({ id: "w1", order_position: 1 }), row({ id: "c1", status: "called", order_position: null })],
      ["s1", "c1", "w1", "w2"],
    ],
    [
      "call order wins over the time people joined",
      [
        row({ id: "late", order_position: 1, joined_at: "2026-11-02T10:00:00+05:30" }),
        row({ id: "early", order_position: 2, joined_at: "2026-11-02T09:00:00+05:30" }),
      ],
      ["late", "early"],
    ],
    ["a waiting token with no call order goes last", [row({ id: "unknown", order_position: null }), row({ id: "known", order_position: 3 })], ["known", "unknown"]],
  ])("%s", (_, tokens, expected) => {
    expect(consoleOrder(tokens).map((token) => token.id)).toEqual(expected);
  });
});

describe("a console row", () => {
  const NOW = new Date("2026-11-02T09:20:00+05:30").getTime();

  it("shows the number, name, service, source, priority, skips and the wait in the business zone", () => {
    show(<QueueRow token={row({ priority: 2, skip_count: 1, service_name: "Checkup", source: "appointment" })} zone={ZONE} now={NOW} />);
    expect(screen.getByText("#1")).toBeTruthy();
    expect(screen.getByText("Asha Customer")).toBeTruthy();
    expect(screen.getByText("Checkup")).toBeTruthy();
    expect(screen.getByText("Appointment")).toBeTruthy();
    expect(screen.getByText("Urgent")).toBeTruthy();
    expect(screen.getByText("Skipped 1×")).toBeTruthy();
    // 09:05 in Kolkata, not 19:35 the day before in Los Angeles.
    expect(screen.getByText(/Waiting 15 min · joined 9:05 AM \(Asia\/Kolkata\)/)).toBeTruthy();
    expect(screen.getByText("Waiting")).toBeTruthy();
  });

  it("a walk-in with no service says so", () => {
    show(<QueueRow token={row()} zone={ZONE} now={NOW} />);
    expect(screen.getByText("Service not chosen yet")).toBeTruthy();
    expect(screen.getByText("Walk-in")).toBeTruthy();
  });

  it("renders no field beyond the ones it is given, whatever else the token carries", () => {
    const extra = {
      ...row({ service_name: "Checkup" }),
      phone: "+919876543210",
      email: "asha@example.test",
      customer: { id: "c1", name: "Asha Customer", phone: "+919876543210", email: "asha@example.test" },
      predicted_wait_sec: 1234,
    } as unknown as SnapshotToken;
    const { container } = show(<QueueRow token={extra} zone={ZONE} now={NOW} />);
    const text = container.textContent ?? "";
    for (const secret of ["+91", "9876543210", "asha@example.test", "1234", "c1"]) {
      expect(text, secret).not.toContain(secret);
    }
  });
});

describe("the queue status menu", () => {
  it("pausing sends one PATCH and never guesses the new status", async () => {
    // The response is held, so the screen is inspected while the press is still out: that is
    // the only moment an optimistic update would be visible.
    const held: (() => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, method: init.method ?? "GET", key: new Headers(init.headers).get("Idempotency-Key"), body: init.body ? JSON.parse(String(init.body)) : null });
        await new Promise<void>((resolve) => held.push(resolve));
        return new Response(null, { status: 204 });
      }),
    );
    show(<StatusMenu queueId="q1" status="open" />);
    fireEvent.click(screen.getByRole("button", { name: "Paused" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({ method: "PATCH", body: { status: "paused" } });
    expect(calls[0].url).toContain("/v1/queues/q1");
    // No optimistic update: Open is still the only pressed choice until the server says
    // otherwise (the button that was pressed reads "Please wait…" while its request is out).
    const pressed = screen.getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true");
    expect(pressed.map((button) => button.textContent)).toEqual(["Open"]);
    await act(async () => {
      held.forEach((resolve) => resolve());
    });
  });

  it("a double press sends one request", async () => {
    answer = () => new Response(null, { status: 204 });
    show(<StatusMenu queueId="q1" status="open" />);
    const paused = screen.getByRole("button", { name: "Paused" });
    // Both presses inside one act(): no re-render in between, so the in-flight guard decides.
    await act(async () => {
      fireEvent.click(paused);
      fireEvent.click(paused);
    });
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls).toHaveLength(1);
  });

  it("choosing the status it already has does nothing", async () => {
    show(<StatusMenu queueId="q1" status="paused" />);
    await userEvent.click(screen.getByRole("button", { name: "Paused" }));
    expect(calls).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("after 5 s it says so and a retry reuses the same key", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const held: (() => void)[] = []; // the request stays out until the test lets it finish
    answer = () => new Response(null, { status: 204 });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, method: init.method ?? "GET", key: new Headers(init.headers).get("Idempotency-Key"), body: null });
        await new Promise<void>((resolve) => held.push(resolve));
        return new Response(null, { status: 204 });
      }),
    );
    show(<StatusMenu queueId="q1" status="open" />);
    fireEvent.click(screen.getByRole("button", { name: "Paused" }));
    await act(() => vi.advanceTimersByTimeAsync(SLOW_PRESS_MS));
    expect(screen.getByText("Still working… check the list.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Paused" }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(calls).toHaveLength(2);
    expect(calls[1].key).toBe(calls[0].key); // the same action, so the same key
    held.forEach((resolve) => resolve());
  });

  it("closing asks first; Esc keeps the queue open", async () => {
    show(<StatusMenu queueId="q1" status="open" />);
    await userEvent.click(screen.getByRole("button", { name: "Closed" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Close the queue for today?")).toBeTruthy();
    expect(calls).toHaveLength(0);
    fireEvent(dialog, new Event("cancel"));
    (dialog as HTMLDialogElement).close();
    expect(calls).toHaveLength(0); // Esc is not a confirmation
    await userEvent.click(screen.getByRole("button", { name: "Closed" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Yes, close it" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ status: "closed" });
  });

  it("keeping it open from the dialog sends nothing", async () => {
    show(<StatusMenu queueId="q1" status="open" />);
    await userEvent.click(screen.getByRole("button", { name: "Closed" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Keep it open" }));
    expect(calls).toHaveLength(0);
  });

  it("a refusal is explained in the console's own words", async () => {
    answer = () => new Response(JSON.stringify({ code: "INVALID_TRANSITION" }), { status: 409, headers: { "Content-Type": "application/json" } });
    show(<StatusMenu queueId="q1" status="paused" />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect((await screen.findByRole("alert")).textContent).toContain("This queue is closed for today, so its status can't change again.");
  });
});

describe("the business picker", () => {
  const two = [
    { id: "b1", name: "North Clinic", role: "owner", category: "clinic", address: null, timezone: ZONE, open_hours: {} },
    { id: "b2", name: "South Clinic", role: "staff", category: "clinic", address: null, timezone: ZONE, open_hours: {} },
  ];
  const serve = (list: unknown[]) => {
    answer = () => new Response(JSON.stringify(list), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  it("one business goes straight to its console", async () => {
    serve([two[0]]);
    show(<BusinessPicker />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/console/b1"));
  });

  it("several are listed with the role", async () => {
    serve(two);
    show(<BusinessPicker />);
    expect(await screen.findByText("North Clinic")).toBeTruthy();
    expect(screen.getByText("South Clinic")).toBeTruthy();
    expect(screen.getByText("Owner")).toBeTruthy();
    expect(screen.getByText("Staff")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("the remembered business opens again", async () => {
    serve(two);
    rememberBusiness("b2");
    show(<BusinessPicker />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/console/b2"));
  });

  it("a remembered business that is not in the list is ignored", async () => {
    serve(two);
    rememberBusiness("gone");
    show(<BusinessPicker />);
    expect(await screen.findByText("North Clinic")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("signing out clears the remembered business", async () => {
    rememberBusiness("b2");
    const { logout } = await import("@/lib/auth/session");
    await logout(); // the real path: ending the session forgets it
    serve(two);
    show(<BusinessPicker />);
    expect(await screen.findByText("North Clinic")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("what the console remembers", () => {
  const mine = [{ id: "b1", name: "North Clinic", role: "staff", category: "clinic", address: null, timezone: ZONE, open_hours: {} }];

  function serveConsole() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, method: init.method ?? "GET", key: null, body: null });
        if (url.includes("/businesses/mine")) return Response.json(mine);
        if (url.endsWith("/queue")) return Response.json({ id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2026-11-02", status: "open" });
        if (url.includes("/queues/q1") && !url.includes("stream")) {
          return Response.json({ id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2026-11-02", status: "open", counts: { completed: 0, no_show: 0, cancelled: 0 }, tokens: [] });
        }
        return new Response("{}", { status: 500 }); // the stream: not needed here
      }),
    );
  }

  it("remembers a business the account's own list confirms", async () => {
    serveConsole();
    show(<QueueConsole businessId="b1" />);
    expect(await screen.findByRole("heading", { name: "North Clinic" })).toBeTruthy();
    expect(rememberedBusiness()).toBe("b1");
  });

  it("a deep link to a business that is not in the list remembers nothing", async () => {
    serveConsole();
    show(<QueueConsole businessId="someone-elses" />);
    // A business that was never ours reads neutrally (6D-2); "access has ended" is for one taken away.
    expect(await screen.findByRole("heading", { name: "You don't have access to this business." })).toBeTruthy();
    expect(rememberedBusiness()).toBeNull();
  });
});

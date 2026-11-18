import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueueSnapshot, SnapshotToken } from "@/lib/api/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), usePathname: () => "/console", useSearchParams: () => new URLSearchParams(window.location.search) }));

const { ActionBar } = await import("@/components/console/ActionBar");
const { TokenDetail } = await import("@/components/console/TokenDetail");
const { QueueConsole } = await import("@/components/console/QueueConsole");
const { AccessEnded } = await import("@/components/console/AccessEnded");
const { MAX_SKIPS, PRIMARY_HOLD_MS, PRIMARY_LABEL, actionAnnouncement, actionMessage, changedNotice, endedNotice, holdOnChange, primaryAction, skipLabel } =
  await import("@/lib/console/actions");
const { useAction } = await import("@/lib/console/useAction");

HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

const ZONE = "Asia/Kolkata";

const row = (over: Partial<SnapshotToken> = {}): SnapshotToken => ({
  id: "t1",
  token_no: 12,
  status: "waiting",
  priority: 0,
  source: "walk_in",
  skip_count: 0,
  version: 3,
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

const snapshotOf = (tokens: SnapshotToken[], status = "open"): QueueSnapshot => ({
  id: "q1",
  business_id: "b1",
  resource_id: "r1",
  queue_date: "2026-11-02",
  status,
  counts: { completed: 0, no_show: 0, cancelled: 0 },
  tokens,
});

type Call = { url: string; method: string; key: string | null; body: Record<string, unknown> | null };
let calls: Call[];
let answers: ((call: Call) => Response)[];
let tokens: SnapshotToken[];
let held: (() => void)[];
let hold: boolean;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const call: Call = {
        url,
        method: init.method ?? "GET",
        key: new Headers(init.headers).get("Idempotency-Key"),
        body: init.body ? JSON.parse(String(init.body)) : null,
      };
      calls.push(call);
      if (hold) await new Promise<void>((resolve) => held.push(resolve));
      if (url.includes("/businesses/mine")) {
        return Response.json([{ id: "b1", name: "North Clinic", role: "staff", category: "clinic", address: null, timezone: ZONE, open_hours: {} }]);
      }
      if (url.includes("/services")) {
        // The endpoint leaves inactive services out unless they are asked for (owner only).
        const list = [{ id: "sv1", business_id: "b1", name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00", is_active: true }];
        if (url.includes("include_inactive")) {
          list.push({ id: "sv0", business_id: "b1", name: "Retired checkup", duration_min: 30, buffer_min: 0, price: "250.00", is_active: false });
        }
        return Response.json(list);
      }
      if (url.endsWith("/queue")) return Response.json({ id: "q1", business_id: "b1", resource_id: "r1", queue_date: "2026-11-02", status: "open" });
      if (url.includes("/queues/q1/stream-token")) return Response.json({ stream_token: "s1", expires_in: 60 });
      // A real stream the test can write deltas into: one connection, so nothing re-reads the
      // snapshot behind the test. The reconnect rules are covered in live-queue.test.tsx.
      if (url.includes("/queues/q1/stream")) {
        const body = new ReadableStream<Uint8Array>({
          start: (controller) => {
            feed = (text: string) => controller.enqueue(new TextEncoder().encode(text));
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.includes("/queues/q1") && call.method === "GET") return Response.json(snapshotOf(tokens));
      const answer = answers.shift();
      const response = answer ? answer(call) : Response.json(row({ status: "called", version: 4 }));
      // Keep the fake snapshot in step with what the action did, as the server would: otherwise
      // the next snapshot read would undo it and look like someone else's change.
      if (response.ok && response.status !== 204) {
        const token = (await response.clone().json()) as SnapshotToken | null;
        if (token?.id) tokens = ["waiting", "called", "in_service"].includes(token.status) ? [token] : [];
      }
      return response;
    }),
  );
}

let client: QueryClient;
function show(node: ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

let feed: (text: string) => void;
let eventId = 100;

/** Someone else's change arriving the way it really does: a delta on the staff stream. */
async function remoteChange(next: SnapshotToken[]) {
  tokens = next;
  const [token] = next;
  eventId += 1;
  const envelope = {
    id: eventId,
    type: `token.${{ in_service: "started", called: "called", completed: "completed", no_show: "no_show", cancelled: "cancelled" }[token.status] ?? "joined"}`,
    queue_id: "q1",
    data: { ...token, token_id: token.id, customer: token.customer },
  };
  await act(async () => {
    feed(`id: ${eventId}\nevent: ${envelope.type}\ndata: ${JSON.stringify(envelope)}\n\n`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}

/** The row is a button whose name is its whole content, so match how it starts. */
const selectRow = (no: number) => screen.getByRole("button", { name: new RegExp(`^#${no}(?!\\d)`) });

const bar = (over: Record<string, unknown> = {}) => (
  <ActionBar businessId="b1" target={undefined} busy={false} slow={false} error={null} onPress={() => {}} {...over} />
);

beforeEach(() => {
  calls = [];
  answers = [];
  eventId = 100;
  held = [];
  hold = false;
  tokens = [row()];
  serve();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the primary action", () => {
  it.each([
    ["no target at all", undefined, "call-next"],
    ["a waiting target", row({ status: "waiting" }), "call-next"],
    ["a called target", row({ status: "called" }), "start"],
    ["an in-service target", row({ status: "in_service" }), "complete"],
    ["a completed target", row({ status: "completed" }), "call-next"],
    ["a no-show target", row({ status: "no_show" }), "call-next"],
    ["a cancelled target", row({ status: "cancelled" }), "call-next"],
  ] as const)("%s gives %s", (_, target, expected) => {
    expect(primaryAction(target)).toBe(expected);
  });

  it("an empty queue still offers Call next", () => {
    show(bar({ target: undefined }));
    expect(screen.getByRole("button", { name: PRIMARY_LABEL["call-next"] })).toBeTruthy();
  });

  it("Call next presses even while the queue is paused", async () => {
    const pressed: unknown[] = [];
    show(bar({ onPress: (press: unknown) => pressed.push(press) }));
    await userEvent.click(screen.getByRole("button", { name: "Call next" }));
    expect(pressed).toEqual([{ kind: "call-next" }]);
  });
});

describe("the skip label", () => {
  it.each([
    [0, `Skip (${MAX_SKIPS}/${MAX_SKIPS})`],
    [1, `Skip (1/${MAX_SKIPS})`],
    [2, "No-show"],
    [3, "No-show"],
  ])("skip_count %i reads %s", (count, expected) => {
    expect(skipLabel(row({ status: "called", skip_count: count }))).toBe(expected);
  });

  it("matches the server's own rule: the skip after MAX_SKIPS is the no-show", () => {
    expect(MAX_SKIPS).toBe(2); // backend transitions.py
    expect(skipLabel(row({ status: "called", skip_count: MAX_SKIPS }))).toBe("No-show");
  });
});

describe("the service picker", () => {
  it("a walk-in Start needs a service, and the button waits for it", async () => {
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called", appointment_id: null }), onPress: (p: unknown) => pressed.push(p) }));
    const start = screen.getByRole("button", { name: "Start" });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    const picker = await screen.findByLabelText("Service for this walk-in");
    expect(await screen.findByRole("option", { name: "Checkup" })).toBeTruthy();
    // A service nobody can be started with must never be offered.
    expect(screen.queryByRole("option", { name: "Retired checkup" })).toBeNull();
    const asked = calls.filter((call) => call.url.includes("/services"));
    expect(asked).toHaveLength(1);
    expect(asked[0].url).not.toContain("include_inactive");
    await userEvent.selectOptions(picker, "sv1");
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(pressed).toEqual([{ kind: "start", tokenId: "t1", serviceId: "sv1" }]);
  });

  it("an appointment token has no picker, and sends no service", async () => {
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called", appointment_id: "a1", source: "appointment" }), onPress: (p: unknown) => pressed.push(p) }));
    expect(screen.queryByLabelText("Service for this walk-in")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(pressed).toEqual([{ kind: "start", tokenId: "t1", serviceId: null }]);
    expect(calls.some((call) => call.url.includes("/services"))).toBe(false); // not even fetched
  });
});

describe("the action bar's other rules", () => {
  it("Skip and No-show are offered only for a called token", () => {
    show(bar({ target: row({ status: "waiting" }) }));
    expect((screen.getByRole("button", { name: "Skip" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "No-show" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("the priority control is offered for a waiting token only", () => {
    const { unmount } = show(bar({ target: row({ status: "waiting" }) }));
    expect(screen.getByRole("group", { name: "Priority" })).toBeTruthy();
    unmount();
    for (const status of ["called", "in_service"]) {
      const view = show(bar({ target: row({ status }) }));
      expect(screen.queryByRole("group", { name: "Priority" })).toBeNull();
      view.unmount();
    }
  });

  it("no-show asks first; Esc keeps the token and sends nothing", async () => {
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called" }), onPress: (p: unknown) => pressed.push(p) }));
    await userEvent.click(screen.getByRole("button", { name: "No-show" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Mark #12 as not here?")).toBeTruthy();
    fireEvent(dialog, new Event("cancel"));
    (dialog as HTMLDialogElement).close();
    expect(pressed).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "No-show" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Yes, not here" }));
    expect(pressed).toEqual([{ kind: "no-show", tokenId: "t1" }]);
  });

  it.each([
    ["Esc", (dialog: HTMLElement) => {
      fireEvent(dialog, new Event("cancel"));
      (dialog as HTMLDialogElement).close();
    }],
    ["Keep waiting", (dialog: HTMLElement) => {
      within(dialog).getByRole("button", { name: "Keep waiting" }).click();
    }],
  ])("the no-show dialog gives focus back to No-show after %s", async (_, dismiss) => {
    // Focus starts inside the dialog, as a real modal puts it, so the assertion below can only
    // pass because the code restores it: jsdom's <dialog> stand-in moves focus by itself.
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called" }), onPress: (p: unknown) => pressed.push(p) }));
    const open = screen.getByRole("button", { name: "No-show" });
    await userEvent.click(open);
    const dialog = screen.getByRole("dialog");
    within(dialog).getByRole("button", { name: "Keep waiting" }).focus();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await act(async () => {
      dismiss(dialog);
    });
    expect(document.activeElement).toBe(open);
    expect(pressed).toEqual([]);
  });

  it("a confirmed no-show leaves focus where the code puts it: on the dialog's own button", async () => {
    // Recorded as it is today, not as an opinion: the confirm path does not restore focus,
    // because the action bar relabels itself and the opener may be gone.
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called" }), onPress: (p: unknown) => pressed.push(p) }));
    await userEvent.click(screen.getByRole("button", { name: "No-show" }));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", { name: "Yes, not here" });
    await userEvent.click(confirm);
    expect(pressed).toEqual([{ kind: "no-show", tokenId: "t1" }]);
    expect(document.activeElement).toBe(confirm);
  });

  it("the last skip is labelled No-show and confirmed", async () => {
    const pressed: unknown[] = [];
    show(bar({ target: row({ status: "called", skip_count: MAX_SKIPS }), onPress: (p: unknown) => pressed.push(p) }));
    const skip = screen.getAllByRole("button", { name: "No-show" })[0];
    await userEvent.click(skip);
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(pressed).toEqual([]); // never a bare skip
  });

  it("while a press is out the bar is disabled, then says so after 5 s", async () => {
    const { rerender } = show(bar({ target: row({ status: "called", appointment_id: "a1" }), busy: true }));
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(true);
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <ActionBar businessId="b1" target={row({ status: "called", appointment_id: "a1" })} busy slow error={null} onPress={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Still working… check the list.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("what the staff member is told", () => {
  it.each([
    ["call-next", 12, "Called #12"],
    ["call-next", null, "Nobody is waiting"],
    ["start", 12, "Started #12"],
    ["complete", 12, "Completed #12"],
    ["skip", 12, "Skipped #12"],
    ["no-show", 12, "Marked #12 no-show"],
    ["priority", 12, "Priority set"],
  ] as const)("%s of %s reads %s", (action, tokenNo, expected) => {
    expect(actionAnnouncement(action, tokenNo)).toBe(expected);
  });

  it.each([
    ["completed", "#12 completed"],
    ["no_show", "#12 no-show"],
    ["cancelled", "#12 cancelled"],
    [undefined, "#12 is no longer in the queue"],
  ])("a token that left with %s reads %s", (status, expected) => {
    expect(endedNotice(12, status)).toBe(expected);
  });

  it.each([
    ["VERSION_CONFLICT", 409, "Someone else changed this"],
    ["INVALID_TRANSITION", 409, "That is no longer possible"],
    ["NOT_FOUND", 404, "isn't there any more"],
    ["FORBIDDEN", 403, "staff and owner"],
    ["QUEUE_CLOSED", 409, "isn't today's queue"],
    ["VALIDATION_ERROR", 422, "Pick a service"],
    ["SERVICE_UNAVAILABLE", 503, "can't do that right now"],
  ])("%s is explained in the console's own words", async (code, status, expected) => {
    const { ApiError } = await import("@/lib/api/client");
    expect(actionMessage(new ApiError(status, code, null, {}))).toContain(expected);
  });

  it("a rate limit keeps its countdown", async () => {
    const { ApiError } = await import("@/lib/api/client");
    expect(actionMessage(new ApiError(429, "RATE_LIMITED", 90, {}))).toContain("2 minutes");
  });
});

describe("the detail panel", () => {
  it("shows the token's own fields, in the business zone", () => {
    show(<TokenDetail token={row({ status: "called", called_at: "2026-11-02T09:20:00+05:30", service_name: "Checkup", priority: 1 })} zone={ZONE} />);
    expect(screen.getByRole("heading", { name: "#12" })).toBeTruthy();
    expect(screen.getByText("Asha Customer")).toBeTruthy();
    expect(screen.getByText("Checkup")).toBeTruthy();
    expect(screen.getByText("Priority", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText(/9:05 AM \(Asia\/Kolkata\)/)).toBeTruthy();
    expect(screen.getByText(/9:20 AM \(Asia\/Kolkata\)/)).toBeTruthy();
    expect(screen.queryByText(/Started/)).toBeNull(); // not set, not shown
  });

  it("a walk-in with no service says so", () => {
    show(<TokenDetail token={row()} zone={ZONE} />);
    expect(screen.getByText("Service not chosen yet")).toBeTruthy();
  });

  it("renders nothing the token happens to carry beyond its own fields", () => {
    const extra = {
      ...row(),
      phone: "+919876543210",
      email: "asha@example.test",
      customer: { id: "c1", name: "Asha Customer", phone: "+919876543210" },
      predicted_wait_sec: 4321,
    } as unknown as SnapshotToken;
    const { container } = show(<TokenDetail token={extra} zone={ZONE} />);
    const text = container.textContent ?? "";
    for (const secret of ["+91", "9876543210", "asha@example.test", "4321", "c1"]) {
      expect(text, secret).not.toContain(secret);
    }
  });
});

describe("access to a business", () => {
  it("a business that was taken away says so", () => {
    show(<AccessEnded />);
    expect(screen.getByRole("heading", { name: "Your access to this business has ended" })).toBeTruthy();
  });

  it("a business that was never ours is put neutrally", () => {
    show(<AccessEnded reason="missing" />);
    expect(screen.getByRole("heading", { name: "You don't have access to this business." })).toBeTruthy();
    expect(screen.getByText(/isn't one of the businesses you work at/)).toBeTruthy();
  });

  it("the console uses the neutral words for a business that is not in the list", async () => {
    show(<QueueConsole businessId="not-mine" />);
    expect(await screen.findByRole("heading", { name: "You don't have access to this business." })).toBeTruthy();
  });
});

describe("the console's requests", () => {
  async function console_() {
    const view = show(<QueueConsole businessId="b1" />);
    expect(await screen.findByRole("heading", { name: "North Clinic" }, { timeout: 5000 })).toBeTruthy();
    return view;
  }

  it("Call next sends one keyed POST and says what was called", async () => {
    tokens = [row()];
    answers = [() => Response.json(row({ status: "called", version: 4 }))];
    await console_();
    await userEvent.click(screen.getByRole("button", { name: "Call next" }));
    await waitFor(() => expect(calls.some((call) => call.url.includes("/call-next"))).toBe(true));
    const [call] = calls.filter((item) => item.url.includes("/call-next"));
    expect(call.method).toBe("POST");
    expect(call.key).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Called #12"));
  });

  it("204 means nobody is waiting, not an error", async () => {
    tokens = [];
    answers = [() => new Response(null, { status: 204 })];
    await console_();
    await userEvent.click(screen.getByRole("button", { name: "Call next" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Nobody is waiting"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("after a VERSION_CONFLICT nothing retries by itself, and the next press is a new key", async () => {
    tokens = [row({ status: "called", version: 3, appointment_id: "a1", source: "appointment" })];
    answers = [
      () => Response.json({ code: "VERSION_CONFLICT" }, { status: 409 }),
      () => Response.json(row({ status: "in_service", version: 5, appointment_id: "a1" })),
    ];
    await console_();
    await userEvent.click(selectRow(12));
    // The server has already moved on, which is why this screen's version will be refused.
    tokens = [row({ status: "called", version: 4, appointment_id: "a1", source: "appointment" })];
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    const starts = () => calls.filter((call) => call.url.includes("/start"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Someone else changed this"));
    expect(starts()).toHaveLength(1); // never retried on its own
    const first = starts()[0];
    expect(first.body).toMatchObject({ version: 3 });
    // The conflict refetched the snapshot, so a deliberate press now sends the fresh version.
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(starts()).toHaveLength(2));
    const second = starts()[1];
    expect(second.body).toMatchObject({ version: 4 });
    expect(second.key).not.toBe(first.key);
  });

  it("a retry of the same press reuses its key", async () => {
    tokens = [row({ status: "called", version: 3, appointment_id: "a1", source: "appointment" })];
    await console_();
    await userEvent.click(selectRow(12));
    hold = true;
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText("Still working… check the list.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    const starts = calls.filter((call) => call.url.includes("/start"));
    expect(starts).toHaveLength(2);
    expect(starts[1].key).toBe(starts[0].key); // the same press, so the same key
    held.forEach((resolve) => resolve());
  });

  it.each([
    ["Call next", "call-next", undefined],
    ["Start", "start", "a1"],
    ["Complete", "complete", "a1"],
  ] as const)("a double press of %s sends one request", async (label, path, appointment) => {
    const status = path === "start" ? "called" : path === "complete" ? "in_service" : "waiting";
    tokens = [row({ status, appointment_id: appointment ?? null, source: appointment ? "appointment" : "walk_in" })];
    await console_();
    if (path !== "call-next") await userEvent.click(selectRow(12));
    hold = true;
    const button = screen.getByRole("button", { name: label });
    // Both presses inside one act(): no re-render in between, so the guard alone decides.
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    const sent = calls.filter((call) => call.url.includes(path === "call-next" ? "/call-next" : `/${path}`));
    expect(sent).toHaveLength(1);
    held.forEach((resolve) => resolve());
  });

  it.each([
    ["skip", "Skip (2/2)"],
    ["no-show", "Yes, not here"],
  ] as const)("a double press of %s sends one request", async (path, label) => {
    tokens = [row({ status: "called" })];
    await console_();
    await userEvent.click(selectRow(12));
    if (path === "no-show") {
      await userEvent.click(screen.getByRole("button", { name: "No-show" }));
    }
    hold = true;
    const button = path === "no-show" ? within(screen.getByRole("dialog")).getByRole("button", { name: label }) : screen.getByRole("button", { name: label });
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(calls.filter((call) => call.url.includes(`/${path}`))).toHaveLength(1);
    held.forEach((resolve) => resolve());
  });

  it("a double press of a priority button sends one request", async () => {
    tokens = [row({ status: "waiting", priority: 0 })];
    await console_();
    await userEvent.click(selectRow(12));
    hold = true;
    const button = screen.getByRole("button", { name: "Urgent" });
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(calls.filter((call) => call.url.includes("/priority"))).toHaveLength(1);
    held.forEach((resolve) => resolve());
  });

  it("priority sends the value and the version, and PATCH", async () => {
    tokens = [row({ status: "waiting", priority: 0, version: 7 })];
    answers = [() => Response.json(row({ status: "waiting", priority: 2, version: 8 }))];
    await console_();
    await userEvent.click(selectRow(12));
    await userEvent.click(screen.getByRole("button", { name: "Urgent" }));
    await waitFor(() => expect(calls.some((call) => call.url.includes("/priority"))).toBe(true));
    const [call] = calls.filter((item) => item.url.includes("/priority"));
    expect(call.method).toBe("PATCH");
    expect(call.body).toEqual({ priority: 2, version: 7 });
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Priority set"));
  });

  it("a token that leaves the queue closes its panel and is accounted for", async () => {
    tokens = [row({ status: "in_service" })];
    await console_();
    await userEvent.click(selectRow(12));
    expect(screen.getByRole("region", { name: "Token #12" })).toBeTruthy();
    answers = [() => Response.json(row({ status: "completed", version: 4 }))];
    await userEvent.click(screen.getByRole("button", { name: "Complete" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Token #12" })).toBeNull());
    // Our own Complete says what it did; the ended notice is for tokens that vanish otherwise.
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Completed #12"));
  });
});

describe("the primary hold", () => {
  const KINDS = ["call-next", "start", "complete"] as const;

  it.each(
    KINDS.flatMap((from) => KINDS.filter((to) => to !== from).map((to) => [from, to] as const)),
  )("a remote change of the primary from %s to %s holds it", (from, to) => {
    expect(holdOnChange(from, to, false)).toBe(true);
  });

  it.each(KINDS)("the same primary kind (%s) never holds", (kind) => {
    expect(holdOnChange(kind, kind, false)).toBe(false);
  });

  it.each(KINDS)("the first render (%s) never holds", (kind) => {
    expect(holdOnChange(null, kind, false)).toBe(false);
  });

  it.each(KINDS.flatMap((from) => KINDS.filter((to) => to !== from).map((to) => [from, to] as const)))(
    "this user's own press from %s to %s never holds",
    (from, to) => {
      expect(holdOnChange(from, to, true)).toBe(false);
    },
  );

  it("says what changed", () => {
    expect(changedNotice(12, "in_service")).toBe("#12 is now in service");
    expect(changedNotice(12, "called")).toBe("#12 is now called");
    expect(changedNotice(12, "waiting")).toBe("#12 is now waiting");
  });

  it("the hold lasts PRIMARY_HOLD_MS", () => {
    expect(PRIMARY_HOLD_MS).toBe(700);
  });
});

describe("the action hook's own guard", () => {
  function harness(blocked: boolean) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["queue", "q1"], snapshotOf([row({ status: "called" })]));
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    return renderHook(() => useAction("q1", async () => {}, blocked), { wrapper });
  }

  it("sends nothing at all while the primary is held, with no disabled attribute involved", async () => {
    const hook = harness(true);
    let outcome: unknown;
    await act(async () => {
      outcome = await hook.result.current.run({ kind: "complete", tokenId: "t1" });
    });
    expect(outcome).toBe("failed");
    expect(calls.filter((call) => call.url.includes("/complete"))).toEqual([]);
  });

  it("sends as usual when it is not held", async () => {
    const hook = harness(false);
    await act(async () => {
      await hook.result.current.run({ kind: "complete", tokenId: "t1" });
    });
    expect(calls.filter((call) => call.url.includes("/complete"))).toHaveLength(1);
  });
});

describe("the primary hold on the screen", () => {
  /** The console, with a snapshot this test can change under it. */
  async function live() {
    const view = show(<QueueConsole businessId="b1" />);
    expect(await screen.findByRole("heading", { name: "North Clinic" }, { timeout: 5000 })).toBeTruthy();
    return view;
  }

  it("a remote change holds the primary, a click inside the hold sends nothing, and 700 ms later it works", async () => {
    tokens = [row({ status: "called", appointment_id: "a1", source: "appointment" })];
    await live();
    await userEvent.click(selectRow(12));
    expect(screen.getByRole("button", { name: "Start" })).toBeTruthy();
    // Someone else starts it: the primary becomes Complete, and is held.
    await remoteChange([row({ status: "in_service", appointment_id: "a1", source: "appointment", version: 4 })]);
    const complete = screen.getByRole("button", { name: "Complete" });
    expect((complete as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("#12 is now in service");
    const before = calls.length;
    await act(async () => {
      fireEvent.click(complete);
    });
    expect(calls).toHaveLength(before); // nothing was sent
    await waitFor(() => expect((screen.getByRole("button", { name: "Complete" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Complete" }));
    });
    const sent = calls.filter((call) => call.url.includes("/complete"));
    expect(sent).toHaveLength(1);
    expect(sent[0].body).toMatchObject({ version: 4 }); // the fresh version
  });

  it("the user's own action never holds the primary", async () => {
    tokens = [row({ status: "called", appointment_id: "a1", source: "appointment" })];
    answers = [() => Response.json(row({ status: "in_service", appointment_id: "a1", version: 4 }))];
    await live();
    await userEvent.click(selectRow(12));
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Complete" })).toBeTruthy());
    expect((screen.getByRole("button", { name: "Complete" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("status").textContent).toContain("Started #12");
  });

  it("the first load never holds the primary", async () => {
    tokens = [row({ status: "called", appointment_id: "a1" })];
    await live();
    await userEvent.click(selectRow(12));
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("status").textContent).not.toContain("is now");
  });

  it("an update that leaves the primary kind alone never holds it", async () => {
    tokens = [row({ status: "called", appointment_id: "a1", version: 3 })];
    await live();
    await userEvent.click(selectRow(12));
    await remoteChange([row({ status: "called", appointment_id: "a1", version: 9, priority: 2 })]);
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("status").textContent).not.toContain("is now");
  });

  it("a second change inside the window restarts the hold instead of stacking", async () => {
    tokens = [row({ status: "called", appointment_id: "a1" })];
    await live();
    await userEvent.click(selectRow(12));
    await remoteChange([row({ status: "in_service", appointment_id: "a1", version: 4 })]);
    expect((screen.getByRole("button", { name: "Complete" }) as HTMLButtonElement).disabled).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 400)); // part way through the first hold
    await remoteChange([row({ status: "called", appointment_id: "a1", version: 5 })]); // it changes back
    // Past where the FIRST window would have ended: still held, because the second change
    // restarted it instead of letting the first timer run out.
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
  });

  it("someone else ending the selected token closes the panel and says what became of it", async () => {
    tokens = [row({ status: "in_service", appointment_id: "a1" })];
    await live();
    await userEvent.click(selectRow(12));
    expect(screen.getByRole("region", { name: "Token #12" })).toBeTruthy();
    // Another staff member completes it: the delta carries the terminal status.
    await remoteChange([row({ status: "completed", appointment_id: "a1", version: 4 })]);
    await waitFor(() => expect(screen.queryByRole("region", { name: "Token #12" })).toBeNull());
    expect(screen.getByRole("status").textContent).toContain("#12 completed");
  });

  it("Skip, No-show and priority are not held", async () => {
    tokens = [row({ status: "called", skip_count: 0, appointment_id: "a1" })];
    await live();
    await userEvent.click(selectRow(12));
    await remoteChange([row({ status: "in_service", appointment_id: "a1", version: 4 })]);
    expect((screen.getByRole("button", { name: "Complete" }) as HTMLButtonElement).disabled).toBe(true);
    // Skip and No-show are disabled here because the token is in service, not because of the
    // hold; a called token shows them enabled while the primary is held.
    await remoteChange([row({ status: "called", skip_count: 0, appointment_id: "a1", version: 5 })]);
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Skip (2/2)" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getAllByRole("button", { name: "No-show" })[0] as HTMLButtonElement).disabled).toBe(false);
  });
});

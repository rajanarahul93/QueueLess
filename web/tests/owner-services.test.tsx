import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/owner/b1/services",
  useSearchParams: () => new URLSearchParams(),
}));

const { OwnerServices } = await import("@/components/owner/OwnerServices");
const { changedFields, formProblems, asForm } = await import("@/lib/owner/services");

// jsdom has <dialog> but not showModal/close.
HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

type Call = { url: string; method: string; key: string | null; body: Record<string, unknown> | null };
let calls: Call[];
let rows: { id: string; name: string; duration_min: number; buffer_min: number; price: string; is_active: boolean }[];
let answer: (call: Call) => Response;
let hold: boolean;
let held: (() => void)[];

const SERVICE = { id: "s1", name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00", is_active: true };

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
      if (call.method === "GET" && url.includes("/services")) return Response.json(rows);
      if (call.method === "GET") return Response.json({ id: "b1", name: "North Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {} });
      if (hold) await new Promise<void>((resolve) => held.push(resolve));
      return answer(call);
    }),
  );
}

const show = (node: ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);

const writes = () => calls.filter((call) => call.method === "POST" || call.method === "PATCH");

beforeEach(() => {
  calls = [];
  held = [];
  hold = false;
  rows = [SERVICE];
  answer = (call) => Response.json({ ...SERVICE, ...(call.body ?? {}) }, { status: call.method === "POST" ? 201 : 200 });
  serve();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// --- the rules, on their own ---

describe("the service rules", () => {
  it.each([
    [{ name: "", duration_min: "30", buffer_min: "0", price: "1" }, "name", "Give the service a name."],
    [{ name: "x".repeat(101), duration_min: "30", buffer_min: "0", price: "1" }, "name", "Keep the name to 100 characters."],
    [{ name: "A", duration_min: "0", buffer_min: "0", price: "1" }, "duration_min", "A duration from 1 to 1440 minutes."],
    [{ name: "A", duration_min: "1441", buffer_min: "0", price: "1" }, "duration_min", "A duration from 1 to 1440 minutes."],
    [{ name: "A", duration_min: "30", buffer_min: "-1", price: "1" }, "buffer_min", "A buffer from 0 to 1440 minutes."],
    [{ name: "A", duration_min: "30", buffer_min: "0", price: "1.234" }, "price", "A price like 250 or 250.00."],
    [{ name: "A", duration_min: "30", buffer_min: "0", price: "abc" }, "price", "A price like 250 or 250.00."],
  ])("says what is wrong with %s", (form, field, copy) => {
    expect(formProblems(form)[field as "name"]).toBe(copy);
  });

  it("accepts the server's own limits exactly", () => {
    expect(formProblems({ name: "x".repeat(100), duration_min: "1440", buffer_min: "1440", price: "99999999.99" })).toEqual({});
    expect(formProblems({ name: "A", duration_min: "1", buffer_min: "0", price: "0" })).toEqual({});
  });

  it("sends only what changed, and compares the price as a string", () => {
    expect(changedFields(SERVICE, asForm(SERVICE))).toEqual({});
    expect(changedFields(SERVICE, { ...asForm(SERVICE), price: "250.0" })).toEqual({ price: "250.0" });
    expect(changedFields(SERVICE, { ...asForm(SERVICE), name: " Checkup " })).toEqual({}); // trimmed, so unchanged
    expect(changedFields(SERVICE, { ...asForm(SERVICE), duration_min: "45" })).toEqual({ duration_min: 45 });
  });
});

// --- the screen ---

describe("the owner services screen", () => {
  it("lists what there is, with the duration, buffer and price", async () => {
    rows = [SERVICE, { id: "s2", name: "Deep clean", duration_min: 60, buffer_min: 15, price: "900.00", is_active: false }];
    show(<OwnerServices businessId="b1" />);
    expect(await screen.findByText("Checkup")).toBeTruthy();
    expect(screen.getByText("30 min · 250.00")).toBeTruthy();
    expect(screen.getByText("60 min + 15 min buffer · 900.00")).toBeTruthy();
    expect(screen.getAllByText("Offered")).toHaveLength(1);
    expect(screen.getByText("Not offered")).toBeTruthy();
    // the owner's list is the one that includes the inactive services
    expect(calls.some((call) => call.url.includes("include_inactive=true"))).toBe(true);
  });

  it("says so when there are none", async () => {
    rows = [];
    show(<OwnerServices businessId="b1" />);
    expect(await screen.findByText("No services yet. Add the first one people can book.")).toBeTruthy();
  });

  it("a list that fails offers Retry, and the retry reads again", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 })));
    show(<OwnerServices businessId="b1" />);
    expect(await screen.findByText("We can't do that right now. Please try again in a moment.")).toBeTruthy();
    const again: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      again.push(String(url));
      return Response.json(url.includes("/services") ? rows : { id: "b1", name: "North Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {} });
    }));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Checkup")).toBeTruthy();
    expect(again.some((url) => url.includes("/services"))).toBe(true);
  });

  it("adds a service, keyed, and reads the list again", async () => {
    rows = [];
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Add a service" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Checkup" } });
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "250.00" } });
    rows = [SERVICE]; // what the server has when the screen reads again
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    const sent = writes();
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe("POST");
    expect(sent[0].body).toEqual({ name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00" });
    expect(sent[0].key).toMatch(/[0-9a-f-]{36}/);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Added Checkup"));
    expect(calls.filter((call) => call.method === "GET" && call.url.includes("/services")).length).toBeGreaterThan(1);
  });

  it("refuses a bad form before sending, and focuses the first field in error", async () => {
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Add a service" }));
    fireEvent.change(screen.getByLabelText("Duration (minutes)"), { target: { value: "0" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    expect(writes()).toHaveLength(0);
    expect(screen.getByText("Give the service a name.")).toBeTruthy();
    expect(screen.getByText("A duration from 1 to 1440 minutes.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Name"));
    expect(screen.getByLabelText("Name").getAttribute("aria-describedby")).toBe("service-name-error");
  });

  it("an edit sends only the fields that changed", async () => {
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    fireEvent.change(screen.getByLabelText("Duration (minutes)"), { target: { value: "45" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe("PATCH");
    expect(writes()[0].body).toEqual({ duration_min: 45 }); // not the name, not the price
    expect(writes()[0].url).toContain("/services/s1");
  });

  it("an edit that changed nothing sends nothing", async () => {
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    expect(writes()).toHaveLength(0);
  });

  it("a double submit sends one request", async () => {
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Check-up" } });
    const form = screen.getByRole("button", { name: "Save" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(writes()).toHaveLength(1);
  });

  it("a retry of the same press reuses its key; a changed body gets a new one", async () => {
    answer = () => Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 });
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Check-up" } });
    const form = screen.getByRole("button", { name: "Save" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
    });
    await act(async () => {
      fireEvent.submit(form); // the same press again: the same key
    });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Checkup plus" } });
    await act(async () => {
      fireEvent.submit(form); // a different body: a new key
    });
    const keys = writes().map((call) => call.key);
    expect(keys).toHaveLength(3);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("puts the server's complaint on the field it names", async () => {
    answer = () =>
      Response.json(
        { code: "VALIDATION_ERROR", errors: [{ loc: ["body", "price"], msg: "too many digits", type: "value_error" }] },
        { status: 422 },
      );
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "251.00" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    expect(await screen.findByText("Check the price.")).toBeTruthy();
    expect(screen.getByLabelText("Price").getAttribute("aria-invalid")).toBe("true");
  });

  it.each([
    [429, { code: "RATE_LIMITED" }, "Too many attempts. Please wait and try again."],
    [409, { code: "IDEMPOTENCY_CONFLICT" }, "Your booking is still being processed. Check your appointments before trying again."],
    [403, { code: "FORBIDDEN" }, "You don't have access to this."],
  ])("says what a %s means", async (status, body, copy) => {
    answer = () => Response.json(body, { status });
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Checkup" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Check-up" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
    });
    expect(await screen.findByText(copy)).toBeTruthy();
  });

  it("stopping a service asks first; Esc keeps it and focus comes back", async () => {
    show(<OwnerServices businessId="b1" />);
    const stop = await screen.findByRole("button", { name: "Stop offering Checkup" });
    await userEvent.click(stop);
    const dialog = screen.getByRole("dialog", { name: "Stop offering this service?" });
    expect(within(dialog).getByText(/Appointments already booked keep it/)).toBeTruthy();
    // A real modal takes focus when it opens; jsdom's stand-in does not, so the test does it.
    // Without that, focus never leaves the opener and "focus comes back" proves nothing.
    within(dialog).getByRole("button", { name: "Keep it" }).focus();
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent(dialog, new Event("cancel")); // Esc
    (dialog as HTMLDialogElement).close();
    expect(writes()).toHaveLength(0);
    expect(document.activeElement).toBe(stop);
  });

  it("confirming stops it, and the row says so after the list is read again", async () => {
    show(<OwnerServices businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Stop offering Checkup" }));
    rows = [{ ...SERVICE, is_active: false }];
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop offering it" }));
    });
    expect(writes()).toHaveLength(1);
    expect(writes()[0].body).toEqual({ is_active: false });
    expect(await screen.findByText("Not offered")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Offer Checkup again" })).toBeTruthy();
  });

  it("offering it again switches it back on", async () => {
    rows = [{ ...SERVICE, is_active: false }];
    show(<OwnerServices businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Offer Checkup again" });
    rows = [SERVICE]; // what the server has by the time the screen reads again
    await userEvent.click(press);
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body).toEqual({ is_active: true });
    expect(await screen.findByText("Offered")).toBeTruthy();
  });

  it("a row action in flight ignores a second press", async () => {
    hold = true;
    show(<OwnerServices businessId="b1" />);
    const again = await screen.findByRole("button", { name: "Stop offering Checkup" });
    await userEvent.click(again);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop offering it" }));
    });
    expect(writes()).toHaveLength(1);
    held.forEach((release) => release());
  });
});

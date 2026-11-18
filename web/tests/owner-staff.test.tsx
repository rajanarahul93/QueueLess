import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/owner/b1/staff",
  useSearchParams: () => new URLSearchParams(),
}));

const { OwnerStaff } = await import("@/components/owner/OwnerStaff");
const { staffProblems, MIN_PASSPHRASE } = await import("@/lib/owner/staff");

HTMLDialogElement.prototype.showModal = function showModal() {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close() {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

type Call = { url: string; method: string; key: string | null; body: Record<string, unknown> | null };
let calls: Call[];
let rows: { id: string; name: string; email: string | null }[];
let answer: (call: Call) => Response;

const RAVI = { id: "u1", name: "Ravi", email: "ravi@example.test" };
const FIRST_ENTRY = "a-long-enough-one";

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
      if (call.method === "GET" && url.includes("/staff")) return Response.json(rows);
      if (call.method === "GET") return Response.json({ id: "b1", name: "North Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {} });
      return answer(call);
    }),
  );
}

const show = (node: ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);

const writes = () => calls.filter((call) => call.method !== "GET");

beforeEach(() => {
  calls = [];
  rows = [RAVI];
  answer = (call) => (call.method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({ ...RAVI, ...(call.body ?? {}), role: "staff" }, { status: 201 }));
  serve();
});
afterEach(() => vi.unstubAllGlobals());

async function fill(entry = FIRST_ENTRY) {
  await userEvent.click(await screen.findByRole("button", { name: "Add a staff member" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Meera" } });
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "meera@example.test" } });
  fireEvent.change(screen.getByLabelText("First password"), { target: { value: entry } });
}

describe("the staff rules", () => {
  it.each([
    [{ name: "", email: "a@b.co", entry: FIRST_ENTRY }, "name", "Give the person a name."],
    [{ name: "x".repeat(101), email: "a@b.co", entry: FIRST_ENTRY }, "name", "Keep the name to 100 characters."],
    [{ name: "A", email: "", entry: FIRST_ENTRY }, "email", "An email address they will sign in with."],
    [{ name: "A", email: "nope", entry: FIRST_ENTRY }, "email", "That does not look like an email address."],
    [{ name: "A", email: "a@b.co", entry: "short" }, "entry", "At least 10 characters."],
    [{ name: "A", email: "a@b.co", entry: "x".repeat(129) }, "entry", "At most 128 characters."],
  ])("says what is wrong with %s", (form, field, copy) => {
    expect(staffProblems(form)[field as "name"]).toBe(copy);
  });

  it("accepts the server's own limits exactly", () => {
    expect(MIN_PASSPHRASE).toBe(10);
    expect(staffProblems({ name: "x".repeat(100), email: "a@b.co", entry: "x".repeat(128) })).toEqual({});
    expect(staffProblems({ name: "A", email: "a@b.co", entry: "x".repeat(10) })).toEqual({});
  });
});

describe("the owner staff screen", () => {
  it("lists who there is, with the email they sign in with", async () => {
    rows = [RAVI, { id: "u2", name: "Sunita", email: "sunita@example.test" }];
    show(<OwnerStaff businessId="b1" />);
    expect(await screen.findByText("Ravi")).toBeTruthy();
    expect(screen.getByText("sunita@example.test")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("says so when there is nobody", async () => {
    rows = [];
    show(<OwnerStaff businessId="b1" />);
    expect(await screen.findByText("Nobody yet. Add the first person who will run the queue.")).toBeTruthy();
  });

  it("a list that fails offers Retry", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 })));
    show(<OwnerStaff businessId="b1" />);
    expect(await screen.findByText("We can't do that right now. Please try again in a moment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("adds a staff member, keyed, and reads the list again", async () => {
    rows = [];
    show(<OwnerStaff businessId="b1" />);
    await fill();
    rows = [{ id: "u9", name: "Meera", email: "meera@example.test" }];
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Add" }).closest("form")!);
    });
    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe("POST");
    expect(writes()[0].body).toEqual({ name: "Meera", email: "meera@example.test", initial_password: FIRST_ENTRY });
    expect(writes()[0].key).toMatch(/[0-9a-f-]{36}/);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Added Meera"));
    expect(calls.filter((call) => call.method === "GET" && call.url.includes("/staff")).length).toBeGreaterThan(1);
  });

  it("the first password is never written into the page while it is typed", async () => {
    // It belongs in the input and nowhere else: not in the status line, not in a heading, not
    // in any text node a screen reader or a screenshot would pick up.
    show(<OwnerStaff businessId="b1" />);
    await fill();
    expect((screen.getByLabelText("First password") as HTMLInputElement).value).toBe(FIRST_ENTRY);
    expect(screen.queryByText(FIRST_ENTRY)).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("");
  });

  it("the first password never stays on the page after a submit", async () => {
    show(<OwnerStaff businessId="b1" />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Add" }).closest("form")!);
    });
    // The form is gone on success; reopen it and the field must be empty, never refilled.
    await userEvent.click(await screen.findByRole("button", { name: "Add a staff member" }));
    expect((screen.getByLabelText("First password") as HTMLInputElement).value).toBe("");
    // and it is nowhere in the rendered page
    expect(document.body.innerHTML).not.toContain(FIRST_ENTRY);
  });

  it("the first password is cleared even when the server refuses", async () => {
    answer = () => Response.json({ code: "VALIDATION_ERROR", errors: [{ loc: ["body", "email"], msg: "taken", type: "value_error" }] }, { status: 422 });
    show(<OwnerStaff businessId="b1" />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Add" }).closest("form")!);
    });
    expect(await screen.findByText("Check the email address.")).toBeTruthy();
    expect((screen.getByLabelText("First password") as HTMLInputElement).value).toBe("");
    expect(document.body.innerHTML).not.toContain(FIRST_ENTRY);
  });

  it("the first password field hides what is typed, and can be shown", async () => {
    show(<OwnerStaff businessId="b1" />);
    await fill();
    const field = screen.getByLabelText("First password") as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(field.getAttribute("autocomplete")).toBe("new-password");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect((screen.getByLabelText("First password") as HTMLInputElement).type).toBe("text");
  });

  it("refuses a bad form before sending, and focuses the first field in error", async () => {
    show(<OwnerStaff businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Add a staff member" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "nope" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Add" }).closest("form")!);
    });
    expect(writes()).toHaveLength(0);
    expect(screen.getByText("Give the person a name.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Name"));
  });

  it("a double submit sends one request", async () => {
    show(<OwnerStaff businessId="b1" />);
    await fill();
    const form = screen.getByRole("button", { name: "Add" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(writes()).toHaveLength(1);
  });

  it.each([
    [429, { code: "RATE_LIMITED" }, "Too many attempts. Please wait and try again."],
    [403, { code: "FORBIDDEN" }, "You don't have access to this."],
  ])("says what a %s means", async (status, body, copy) => {
    answer = () => Response.json(body, { status });
    show(<OwnerStaff businessId="b1" />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Add" }).closest("form")!);
    });
    expect(await screen.findByText(copy)).toBeTruthy();
  });

  it("removing asks first; Esc keeps them and focus comes back", async () => {
    show(<OwnerStaff businessId="b1" />);
    const press = await screen.findByRole("button", { name: "Remove Ravi" });
    await userEvent.click(press);
    const dialog = screen.getByRole("dialog", { name: "Remove this person?" });
    expect(within(dialog).getByText(/loses access to this business at once/)).toBeTruthy();
    within(dialog).getByRole("button", { name: "Keep them" }).focus();
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent(dialog, new Event("cancel"));
    (dialog as HTMLDialogElement).close();
    expect(writes()).toHaveLength(0);
    expect(document.activeElement).toBe(press);
  });

  it("confirming removes them, and the list is read again", async () => {
    show(<OwnerStaff businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Remove Ravi" }));
    rows = [];
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove them" }));
    });
    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe("DELETE");
    expect(writes()[0].url).toContain("/staff/u1");
    expect(await screen.findByText("Nobody yet. Add the first person who will run the queue.")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("Removed Ravi");
  });

  it("a removal the server refuses says so and keeps the row", async () => {
    answer = () => Response.json({ code: "NOT_FOUND" }, { status: 404 });
    show(<OwnerStaff businessId="b1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Remove Ravi" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove them" }));
    });
    expect(await screen.findByText("We couldn't find that.")).toBeTruthy();
    expect(screen.getByText("Ravi")).toBeTruthy();
  });
});

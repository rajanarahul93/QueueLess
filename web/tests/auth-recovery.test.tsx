import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A refresh that never reached the server says nothing about the session: the cookie was not
// spent, so the customer is not signed out. These tests drive the real provider against a
// `fetch` that throws (transport), refuses (401) or answers, and watch what the guard does.
const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/me",
}));

const USER = { id: "u1", name: "Asha", email: null, phone: "+919876543210", role: "customer" };

/** Fresh modules per test: the session and its listeners live in module state. */
async function load() {
  vi.resetModules();
  const provider = await import("@/lib/auth/AuthProvider");
  const guard = await import("@/lib/auth/RequireRole");
  const session = await import("@/lib/auth/session");
  return { ...provider, ...guard, session };
}

function answers(...plan: ("throw" | "refuse" | "ok")[]) {
  const seen: string[] = [];
  let at = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      seen.push(String(url));
      const next = plan[Math.min(at, plan.length - 1)];
      at += 1;
      if (next === "throw") throw new TypeError("Load failed");
      if (next === "refuse") return new Response(JSON.stringify({ code: "UNAUTHENTICATED" }), { status: 401, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify({ access_token: "fresh", user: USER }), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return { refreshes: () => seen.filter((url) => url.endsWith("/v1/auth/refresh")) };
}

const show = (AuthProvider: (props: { children: ReactNode }) => ReactNode, Guarded: ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>{Guarded}</AuthProvider>
    </QueryClientProvider>,
  );

beforeEach(() => replace.mockReset());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a refresh that never reached the server", () => {
  it("does not sign anybody out and does not redirect", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, RequireRole } = await load();
    answers("throw");
    show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(replace).not.toHaveBeenCalled();
    expect(screen.queryByText("secret")).toBeNull();
    expect(screen.getByLabelText("Loading")).toBeTruthy(); // still deciding, not signed out
  });

  it("a refused refresh still signs out and redirects", async () => {
    const { AuthProvider, RequireRole } = await load();
    answers("refuse");
    show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login?next=%2Fme"));
    expect(screen.queryByText("secret")).toBeNull();
  });

  it("recovers on a retry, and the sign-in page is never shown", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, RequireRole } = await load();
    const seen = answers("throw", "ok");
    show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // the first backoff step
    });
    expect(screen.getByText("secret")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
    expect(seen.refreshes()).toHaveLength(2);
  });

  it("gives up as unreachable, and Retry recovers", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, RequireRole } = await load();
    const seen = answers("throw", "throw", "throw", "throw");
    show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000); // 1000 + 2000 + 4000
    });
    expect(screen.getByText("We can't reach QueueLess. Check your connection.")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
    expect(seen.refreshes()).toHaveLength(4); // the first try and three retries

    vi.useRealTimers();
    vi.unstubAllGlobals();
    answers("ok");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("secret")).toBeTruthy());
  });

  it("an unmount during the backoff leaves nothing pending", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, RequireRole } = await load();
    answers("throw");
    const view = show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    const complain = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(complain).not.toHaveBeenCalled(); // no state update after unmount
    complain.mockRestore();
  });

  it("another tab's session ends the unreachable state", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, RequireRole, session } = await load();
    answers("throw", "throw", "throw", "throw");
    show(AuthProvider, <RequireRole roles={["customer"]} signInPath="/login">secret</RequireRole>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });
    expect(screen.getByText("We can't reach QueueLess. Check your connection.")).toBeTruthy();
    await act(async () => {
      session.adopt({ accessToken: "from-other-tab", user: USER }, false);
    });
    expect(screen.getByText("secret")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });
});

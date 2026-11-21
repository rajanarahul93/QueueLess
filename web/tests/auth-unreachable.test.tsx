import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The "unreachable" state is what the provider settles on after the silent refresh never got an
// answer (4 tries). The guard has its own tests; these cover the two other readers of the state:
// the header must not offer a sign-out for a session nobody has, and the reschedule page must not
// ask for appointments it has no right to.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/me/reschedule/a1",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const USER = { id: "u1", name: "Asha", email: null, phone: "+919876543210", role: "customer" };

async function load() {
  vi.resetModules();
  const { AuthProvider } = await import("@/lib/auth/AuthProvider");
  const { Header } = await import("@/components/Header");
  const { Reschedule } = await import("@/components/appointments/Reschedule");
  return { AuthProvider, Header, Reschedule };
}

/** Every request, whatever its URL, fails to get an answer (or, in "ok" mode, refreshes). */
function network(mode: "down" | "ok") {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(String(url));
      if (mode === "down") throw new TypeError("Load failed");
      return new Response(JSON.stringify({ access_token: "fresh", user: USER }), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return urls;
}

const show = (AuthProvider: (props: { children: ReactNode }) => ReactNode, node: ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>{node}</AuthProvider>
    </QueryClientProvider>,
  );

async function settleUnreachable() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(7000); // 1000 + 2000 + 4000: the whole backoff ladder
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the unreachable auth state", () => {
  it("the header offers Sign out to a signed-in customer (the control for the next test)", async () => {
    const { AuthProvider, Header } = await load();
    network("ok");
    show(AuthProvider, <Header />);
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeTruthy();
  });

  it("the header shows no Sign out and no user, only the brand and the theme switch", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, Header } = await load();
    network("down");
    show(AuthProvider, <Header />);
    await settleUnreachable();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    expect(screen.queryByText("Asha")).toBeNull();
    expect(screen.getByRole("link", { name: "QueueLess" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Theme:/ })).toBeTruthy();
  });

  it("the reschedule page asks for no appointments while the session is unknown", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { AuthProvider, Reschedule } = await load();
    const urls = network("down");
    show(AuthProvider, <Reschedule id="a1" />);
    await settleUnreachable();
    expect(urls.filter((url) => url.includes("/v1/auth/refresh"))).toHaveLength(4); // it did try to sign in
    expect(urls.filter((url) => url.includes("/appointments"))).toEqual([]); // and asked for nothing else
    expect(screen.queryByText("This appointment can't be moved")).toBeNull(); // not a false "not found"
  });
});

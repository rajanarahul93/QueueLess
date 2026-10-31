import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Each test loads fresh modules: the session lives in module state.
async function load() {
  vi.resetModules();
  const client = await import("@/lib/api/client");
  const session = await import("@/lib/auth/session");
  return { ...client, ...session };
}

type Call = { url: string; init: RequestInit };

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const USER = { id: "u1", name: "Asha", email: null, phone: "+919876543210", role: "customer" };

function fakeServer(handler: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const call = { url, init };
      calls.push(call);
      return handler(call);
    }),
  );
  return calls;
}

function header(call: Call, name: string): string | null {
  return new Headers(call.init.headers).get(name);
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api()", () => {
  it("refreshes once on a 401 and retries with the new token and the SAME idempotency key", async () => {
    const { api, adopt } = await load();
    adopt({ accessToken: "old", user: USER }, false);
    const calls = fakeServer(({ url, init }) => {
      if (url.endsWith("/v1/auth/refresh")) return json(200, { access_token: "new", user: USER });
      const token = new Headers(init.headers).get("Authorization");
      return token === "Bearer new" ? json(201, { ok: true }) : json(401, { code: "UNAUTHENTICATED" });
    });
    const result = await api("/v1/queues/q/tokens", { method: "POST", body: {}, idempotencyKey: "key-1" });
    expect(result).toEqual({ ok: true });
    const joins = calls.filter((call) => call.url.endsWith("/v1/queues/q/tokens"));
    expect(joins.map((call) => header(call, "Idempotency-Key"))).toEqual(["key-1", "key-1"]);
    expect(joins.map((call) => header(call, "Authorization"))).toEqual(["Bearer old", "Bearer new"]);
    expect(calls.filter((call) => call.url.endsWith("/v1/auth/refresh"))).toHaveLength(1);
  });

  it("gives up after one refresh: a second 401 reaches the caller", async () => {
    const { api, adopt, ApiError } = await load();
    adopt({ accessToken: "old", user: USER }, false);
    const calls = fakeServer(({ url }) =>
      url.endsWith("/v1/auth/refresh")
        ? json(200, { access_token: "new", user: USER })
        : json(401, { code: "UNAUTHENTICATED" }),
    );
    await expect(api("/v1/tokens?active=true")).rejects.toBeInstanceOf(ApiError);
    expect(calls.filter((call) => call.url.endsWith("/v1/auth/refresh"))).toHaveLength(1);
    expect(calls).toHaveLength(3);
  });

  it("never refreshes for the auth endpoints", async () => {
    const { api } = await load();
    const calls = fakeServer(() => json(401, { code: "UNAUTHENTICATED" }));
    await expect(
      api("/v1/auth/login", { method: "POST", body: {}, auth: false }),
    ).rejects.toMatchObject({ status: 401, code: "UNAUTHENTICATED" });
    expect(calls).toHaveLength(1);
    expect(header(calls[0], "Authorization")).toBeNull();
  });

  it("reads the problem code and Retry-After", async () => {
    const { api } = await load();
    fakeServer(() => json(429, { code: "RATE_LIMITED", detail: "raw server text" }, { "Retry-After": "24" }));
    await expect(api("/v1/auth/otp/request", { method: "POST", body: {}, auth: false })).rejects.toMatchObject({
      status: 429,
      code: "RATE_LIMITED",
      retryAfterSec: 24,
    });
  });

  it("turns a network failure into code NETWORK", async () => {
    const { api } = await load();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(api("/v1/businesses")).rejects.toMatchObject({ status: 0, code: "NETWORK" });
  });
});

describe("refreshSession()", () => {
  it("N simultaneous 401s cause exactly one refresh call", async () => {
    const { api, adopt } = await load();
    adopt({ accessToken: "old", user: USER }, false);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const calls = fakeServer(async ({ url, init }) => {
      if (url.endsWith("/v1/auth/refresh")) {
        await gate; // keep the refresh in flight while all requests hit their 401
        return json(200, { access_token: "new", user: USER });
      }
      return new Headers(init.headers).get("Authorization") === "Bearer new"
        ? json(200, { ok: true })
        : json(401, { code: "UNAUTHENTICATED" });
    });
    const pending = Array.from({ length: 8 }, (_, index) => api(`/v1/things/${index}`));
    await vi.waitFor(() => expect(calls.filter((call) => !call.url.endsWith("/refresh"))).toHaveLength(8));
    release();
    await Promise.all(pending);
    expect(calls.filter((call) => call.url.endsWith("/v1/auth/refresh"))).toHaveLength(1);
  });

  it("takes the cross-tab lock, and skips its own refresh when another tab refreshed meanwhile", async () => {
    const { refreshSession, adopt, getAccessToken } = await load();
    const requested: string[] = [];
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    vi.stubGlobal("navigator", {
      ...navigator,
      locks: {
        request: async (name: string, task: () => Promise<unknown>) => {
          requested.push(name);
          await held; // another tab holds the lock
          return task();
        },
      },
    });
    const calls = fakeServer(() => json(200, { access_token: "mine", user: USER }));
    const refreshed = refreshSession();
    adopt({ accessToken: "from-other-tab", user: USER }, false); // that tab's broadcast
    release();
    expect(await refreshed).toMatchObject({ accessToken: "from-other-tab" });
    expect(requested).toEqual(["queueless-auth-refresh"]);
    expect(calls).toHaveLength(0);
    expect(getAccessToken()).toBe("from-other-tab");
  });
});

describe("the access token", () => {
  it("is never written to localStorage, sessionStorage or document.cookie", async () => {
    const { refreshSession } = await import("@/lib/auth/session");
    const { verifyOtp } = await import("@/lib/auth/actions");
    fakeServer(({ url }) =>
      json(200, { access_token: url.endsWith("/refresh") ? "token-b" : "token-a", user: USER }),
    );
    await verifyOtp("+919876543210", "123456");
    await refreshSession();
    const stored = [
      ...Object.keys(localStorage).map((key) => localStorage.getItem(key)),
      ...Object.keys(sessionStorage).map((key) => sessionStorage.getItem(key)),
      document.cookie,
    ].join(" ");
    expect(stored).not.toContain("token-a");
    expect(stored).not.toContain("token-b");
  });
});

import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import nextConfig from "../next.config";
import { proxy } from "../proxy";

// Two places set response headers: next.config.ts (the static ones, every response) and proxy.ts
// (the per-request nonce policy, report-only). Together they must give the list below, and the
// full policy must never be enforcing until it is measured and moved on purpose.
async function configured(): Promise<Record<string, string>> {
  const rules = await nextConfig.headers!();
  const all = rules.find((rule) => rule.source === "/:path*");
  expect(all, "one rule for every path").toBeTruthy();
  return Object.fromEntries(all!.headers.map((header) => [header.key.toLowerCase(), header.value]));
}

describe("headers from next.config", () => {
  it("stops MIME sniffing", async () => {
    expect((await configured())["x-content-type-options"]).toBe("nosniff");
  });

  it("denies camera, microphone and geolocation", async () => {
    expect((await configured())["permissions-policy"]).toBe("camera=(), microphone=(), geolocation=()");
  });

  it("enforces ONLY frame-ancestors; nothing else is enforced", async () => {
    // frame-ancestors is ignored in report-only mode, so it has to be enforced on its own.
    expect((await configured())["content-security-policy"]).toBe("frame-ancestors 'none'");
  });

  it("sets no report-only policy here, and no referrer policy (the proxy owns both)", async () => {
    const headers = await configured();
    expect(headers["content-security-policy-report-only"]).toBeUndefined();
    expect(headers["referrer-policy"]).toBeUndefined();
  });
});

describe("headers from the proxy", () => {
  const run = () => proxy(new NextRequest("http://localhost:3000/signin"));

  it("sends the full policy as REPORT-ONLY and never as an enforcing header", () => {
    const response = run();
    const policy = response.headers.get("content-security-policy-report-only")!;
    expect(policy).toContain("default-src 'self'");
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("object-src 'none'");
    expect(response.headers.get("content-security-policy")).toBeNull();
  });

  it("uses a fresh nonce on every request, and passes it to Next on the request", () => {
    const first = run();
    const second = run();
    const nonce = (response: Response) => /'nonce-([^']+)'/.exec(response.headers.get("content-security-policy-report-only")!)![1];
    expect(nonce(first)).not.toBe(nonce(second));
    expect(first.headers.get("x-middleware-request-x-nonce")).toBe(nonce(first));
  });

  it("sends no referrer at all", () => {
    expect(run().headers.get("referrer-policy")).toBe("no-referrer");
  });
});

import { expect, test } from "@playwright/test";

// What the browser is actually sent by the production server: the static headers from
// next.config.ts, and the per-request policy from the proxy, which stays report-only.
for (const path of ["/", "/signin", "/no-access"]) {
  test(`${path}: security headers on the page response`, async ({ request }) => {
    const response = await request.get(path);
    expect(response.ok()).toBe(true);
    const headers = response.headers();
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["permissions-policy"]).toBe("camera=(), microphone=(), geolocation=()");
    expect(headers["referrer-policy"]).toBe("no-referrer");
    // The only ENFORCED policy is frame-ancestors; the full one is report-only.
    expect(headers["content-security-policy"]).toBe("frame-ancestors 'none'");
    const reportOnly = headers["content-security-policy-report-only"];
    expect(reportOnly).toContain("default-src 'self'");
    expect(reportOnly).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
  });
}

test("a static asset is not sniffable either", async ({ request }) => {
  const html = await (await request.get("/signin")).text();
  const asset = /\/_next\/static\/[^"']+\.js/.exec(html)?.[0];
  expect(asset, "the page links a script chunk").toBeTruthy();
  const response = await request.get(asset!);
  expect(response.ok()).toBe(true);
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");
});

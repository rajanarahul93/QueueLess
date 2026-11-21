import { expect, test } from "@playwright/test";

// The manifest and its icons, as the production server sends them. There is deliberately no service
// worker: see the notes in the PR.
test("the manifest and its icons are served and valid", async ({ page, request }) => {
  await page.goto("/signin");
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/manifest.webmanifest");
  const response = await request.get("/manifest.webmanifest");
  expect(response.ok()).toBe(true);
  const manifest = await response.json();
  expect(manifest).toMatchObject({ name: "QueueLess", short_name: "QueueLess", display: "standalone", start_url: "/", theme_color: "#1d4ed8", background_color: "#f8fafc" });
  const wanted = [["192x192", "any"], ["512x512", "any"], ["512x512", "maskable"]];
  for (const [sizes, purpose] of wanted) {
    const icon = manifest.icons.find((item: { sizes: string; purpose?: string }) => item.sizes === sizes && (item.purpose ?? "any") === purpose);
    expect(icon, `${sizes} ${purpose}`).toBeTruthy();
    const image = await request.get(icon.src);
    expect(image.ok()).toBe(true);
    expect(image.headers()["content-type"]).toBe("image/png");
    const bytes = await image.body();
    expect(`${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`).toBe(sizes);
  }
});

test("no service worker is registered", async ({ page }) => {
  await page.goto("/signin");
  await page.reload();
  expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
});

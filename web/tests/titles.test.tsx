import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { globSync, readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BusinessTitle } from "@/components/booking/BusinessDetail";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderTitle(booking: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ id: "b1", name: "Clinic" }), { status: 200, headers: { "Content-Type": "application/json" } })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BusinessTitle id="b1" booking={booking} />
    </QueryClientProvider>,
  );
}

describe("page titles", () => {
  it("the Book page is named before the business loads, then with its name", async () => {
    renderTitle(true);
    expect(document.title).toBe("Book a visit · QueueLess");
    await waitFor(() => expect(document.title).toBe("Book at Clinic · QueueLess"));
  });

  it("the business page shows the business name", async () => {
    renderTitle(false);
    expect(document.title).toBe("Business · QueueLess");
    await waitFor(() => expect(document.title).toBe("Clinic · QueueLess"));
  });

  // A metadata title is unmounted and remounted on every client navigation (the tab is briefly
  // untitled), so each page renders its own <title>, outside any sign-in guard. A new page
  // fails here until it is added with its title.
  const TITLES: Record<string, string> = {
    "app/page.tsx": "<title>Home · QueueLess</title>",
    "app/b/page.tsx": "<title>Find a business · QueueLess</title>",
    "app/b/[id]/page.tsx": "<BusinessTitle id={id} />",
    "app/b/[id]/book/page.tsx": "<BusinessTitle id={id} booking />",
    "app/no-access/page.tsx": "<title>No access · QueueLess</title>",
    "app/(auth)/login/page.tsx": "<title>Sign in with your phone · QueueLess</title>",
    "app/(auth)/signin/page.tsx": "<title>Staff and owner sign in · QueueLess</title>",
    "app/(auth)/register/page.tsx": "<title>Create an owner account · QueueLess</title>",
    "app/(customer)/me/page.tsx": "<title>My appointments · QueueLess</title>",
    "app/(customer)/me/reschedule/[id]/page.tsx": "<RescheduleTitle id={id} />",
    "app/(customer)/ticket/page.tsx": "<title>Your ticket · QueueLess</title>",
    "app/(owner)/owner/page.tsx": "<title>Your business · QueueLess</title>",
    "app/(staff)/console/page.tsx": "<title>Queue console · QueueLess</title>",
  };

  it("every page renders its own title, outside the sign-in guard", () => {
    expect(globSync("app/**/page.tsx").sort()).toEqual(Object.keys(TITLES).sort());
    for (const [page, title] of Object.entries(TITLES)) {
      const source = readFileSync(page, "utf8");
      expect(source, page).toContain(title);
      const guard = source.indexOf("<RequireRole");
      if (guard >= 0) expect(source.indexOf(title), page).toBeLessThan(guard);
    }
    const layouts = globSync("app/**/layout.tsx").filter((layout) => /title|metadata/.test(readFileSync(layout, "utf8")));
    expect(layouts).toEqual([]);
  });
});

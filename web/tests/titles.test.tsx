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
  // untitled), so each page renders its own <title>, outside any sign-in guard.
  it("every page renders its own title", () => {
    const pages = globSync("app/**/page.tsx");
    expect(pages.length).toBeGreaterThanOrEqual(11);
    const untitled = pages.filter((page) => !/<title>|<BusinessTitle /.test(readFileSync(page, "utf8")));
    expect(untitled).toEqual([]);
    const layouts = globSync("app/**/layout.tsx").filter((layout) => /title|metadata/.test(readFileSync(layout, "utf8")));
    expect(layouts).toEqual([]);
  });
});

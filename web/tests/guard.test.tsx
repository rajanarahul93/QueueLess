import { render, screen } from "@testing-library/react";
import { globSync, readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/owner",
}));

let auth: { status: string; user: { role: string; name: string } | null };
vi.mock("@/lib/auth/AuthProvider", () => ({ useAuth: () => auth }));

const { RequireRole } = await import("@/lib/auth/RequireRole");
const OwnerPage = (await import("@/app/(owner)/owner/page")).default;
const StaffPage = (await import("@/app/(staff)/console/page")).default;
const CustomerPage = (await import("@/app/(customer)/ticket/page")).default;

function owner(role: string) {
  auth = { status: "signed-in", user: { role, name: "N" } };
}

beforeEach(() => replace.mockReset());

describe("RequireRole", () => {
  it("shows a skeleton while the session is restored", () => {
    auth = { status: "loading", user: null };
    render(<RequireRole roles={["owner"]} signInPath="/signin">secret</RequireRole>);
    expect(screen.getByLabelText("Loading")).toBeTruthy();
    expect(screen.queryByText("secret")).toBeNull();
  });

  it("sends a signed-out visitor to sign in and back", () => {
    auth = { status: "signed-out", user: null };
    render(<RequireRole roles={["owner"]} signInPath="/signin">secret</RequireRole>);
    expect(replace).toHaveBeenCalledWith("/signin?next=%2Fowner");
    expect(screen.queryByText("secret")).toBeNull();
  });

  it.each([
    ["owner", OwnerPage, "Signed in as N", ["owner"], ["customer", "staff", "admin"]],
    ["staff", StaffPage, "Signed in as N", ["staff", "owner"], ["customer", "admin"]],
    ["customer", CustomerPage, "Your live ticket", ["customer"], ["staff", "owner", "admin"]],
  ] as const)("the %s routes admit only their roles", (_, Page, inside, allowed, refused) => {
    for (const role of allowed) {
      owner(role);
      const { unmount } = render(<Page />);
      expect(screen.getByText(inside)).toBeTruthy();
      unmount();
    }
    for (const role of refused) {
      owner(role);
      const { unmount } = render(<Page />);
      expect(screen.queryByText(inside)).toBeNull();
      expect(screen.getByText("You don't have access to this page")).toBeTruthy();
      unmount();
    }
  });

  // The guard lives in each page (its title must sit outside it), so a new page is protected
  // unless it is on this reviewed list of public pages.
  const PUBLIC = [
    "app/page.tsx",
    "app/b/page.tsx",
    "app/b/[id]/page.tsx",
    "app/no-access/page.tsx",
    "app/(auth)/login/page.tsx",
    "app/(auth)/signin/page.tsx",
    "app/(auth)/register/page.tsx",
  ];

  it("every page that is not public renders RequireRole", () => {
    const unguarded = globSync("app/**/page.tsx").filter(
      (page) => !PUBLIC.includes(page) && !readFileSync(page, "utf8").includes("<RequireRole"),
    );
    expect(unguarded).toEqual([]);
  });
});

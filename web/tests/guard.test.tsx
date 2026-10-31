import { render, screen } from "@testing-library/react";
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
const CustomerPage = (await import("@/app/(customer)/me/page")).default;

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
    ["owner", OwnerPage, ["owner"], ["customer", "staff", "admin"]],
    ["staff", StaffPage, ["staff", "owner"], ["customer", "admin"]],
    ["customer", CustomerPage, ["customer"], ["staff", "owner", "admin"]],
  ] as const)("the %s routes admit only their roles", (_, Page, allowed, refused) => {
    for (const role of allowed) {
      owner(role);
      const { unmount } = render(<Page />);
      expect(screen.getByText("Signed in as N")).toBeTruthy();
      unmount();
    }
    for (const role of refused) {
      owner(role);
      const { unmount } = render(<Page />);
      expect(screen.queryByText("Signed in as N")).toBeNull();
      expect(screen.getByText("You don't have access to this page")).toBeTruthy();
      unmount();
    }
  });
});

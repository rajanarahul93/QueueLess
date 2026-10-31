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
const OwnerLayout = (await import("@/app/(owner)/layout")).default;
const StaffLayout = (await import("@/app/(staff)/layout")).default;
const CustomerLayout = (await import("@/app/(customer)/layout")).default;

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
    ["owner", OwnerLayout, ["owner"], ["customer", "staff", "admin"]],
    ["staff", StaffLayout, ["staff", "owner"], ["customer", "admin"]],
    ["customer", CustomerLayout, ["customer"], ["staff", "owner", "admin"]],
  ] as const)("the %s routes admit only their roles", (_, Layout, allowed, refused) => {
    for (const role of allowed) {
      owner(role);
      const { unmount } = render(<Layout>inside</Layout>);
      expect(screen.getByText("inside")).toBeTruthy();
      unmount();
    }
    for (const role of refused) {
      owner(role);
      const { unmount } = render(<Layout>inside</Layout>);
      expect(screen.queryByText("inside")).toBeNull();
      expect(screen.getByText("You don't have access to this page")).toBeTruthy();
      unmount();
    }
  });
});

import type { ReactNode } from "react";
import { RequireRole } from "@/lib/auth/RequireRole";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <RequireRole roles={["customer"]} signInPath="/login">
      {children}
    </RequireRole>
  );
}

"use client";

import { OwnerHub } from "@/components/owner/OwnerHub";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page() {
  return (
    <>
      <title>Your business · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <OwnerHub />
      </RequireRole>
    </>
  );
}

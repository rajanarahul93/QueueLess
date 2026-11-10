"use client";

import { CreateBusiness } from "@/components/owner/CreateBusiness";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page() {
  return (
    <>
      <title>Create a business · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <CreateBusiness />
      </RequireRole>
    </>
  );
}

"use client";

import { use } from "react";
import { OwnerStaff } from "@/components/owner/OwnerStaff";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page({ params }: { params: Promise<{ businessId: string }> }) {
  const { businessId } = use(params);
  return (
    <>
      <title>Staff · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <OwnerStaff businessId={businessId} />
      </RequireRole>
    </>
  );
}

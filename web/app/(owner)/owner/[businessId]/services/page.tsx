"use client";

import { use } from "react";
import { OwnerServices } from "@/components/owner/OwnerServices";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page({ params }: { params: Promise<{ businessId: string }> }) {
  const { businessId } = use(params);
  return (
    <>
      <title>Services · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <OwnerServices businessId={businessId} />
      </RequireRole>
    </>
  );
}

"use client";

import { use } from "react";
import { OwnerSettings } from "@/components/owner/OwnerSettings";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page({ params }: { params: Promise<{ businessId: string }> }) {
  const { businessId } = use(params);
  return (
    <>
      <title>Settings · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <OwnerSettings businessId={businessId} />
      </RequireRole>
    </>
  );
}

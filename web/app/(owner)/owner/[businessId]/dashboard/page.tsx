"use client";

import { use } from "react";
import { OwnerDashboard } from "@/components/owner/OwnerDashboard";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page({ params }: { params: Promise<{ businessId: string }> }) {
  const { businessId } = use(params);
  return (
    <>
      <title>Dashboard · QueueLess</title>
      <RequireRole roles={["owner"]} signInPath="/signin">
        <OwnerDashboard businessId={businessId} />
      </RequireRole>
    </>
  );
}

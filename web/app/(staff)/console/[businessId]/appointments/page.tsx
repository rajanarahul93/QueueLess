"use client";

import { use } from "react";
import { AppointmentsToday } from "@/components/console/AppointmentsToday";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page({ params }: { params: Promise<{ businessId: string }> }) {
  const { businessId } = use(params);
  return (
    <>
      <title>Appointments today · QueueLess</title>
      <RequireRole roles={["staff", "owner"]} signInPath="/signin">
        <AppointmentsToday businessId={businessId} />
      </RequireRole>
    </>
  );
}

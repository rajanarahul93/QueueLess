"use client";

import { BusinessPicker } from "@/components/console/BusinessPicker";
import { RequireRole } from "@/lib/auth/RequireRole";

function Picker() {
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Choose a business</h1>
      <p className="mt-2 text-body text-muted">You work at more than one. Pick the queue to run.</p>
      <BusinessPicker />
    </section>
  );
}

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page() {
  return (
    <>
      <title>Choose a business · QueueLess</title>
      <RequireRole roles={["staff", "owner"]} signInPath="/signin">
        <Picker />
      </RequireRole>
    </>
  );
}

import { Suspense } from "react";
import { Reschedule, RescheduleTitle } from "@/components/appointments/Reschedule";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default async function ReschedulePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <RescheduleTitle id={id} />
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <Suspense>
            <Reschedule id={id} />
          </Suspense>
        </section>
      </RequireRole>
    </>
  );
}

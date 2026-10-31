import { Suspense } from "react";
import { BookingFlow } from "@/components/booking/BookingFlow";
import { BusinessTitle } from "@/components/booking/BusinessDetail";
import { RequireRole } from "@/lib/auth/RequireRole";

// Free times need a signed-in customer, so signing in comes first (back here afterwards).
export default async function BookPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // The title sits outside the guard, so the tab is named while the session is checked.
  return (
    <>
      <BusinessTitle id={id} booking />
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <Suspense>
            <BookingFlow businessId={id} />
          </Suspense>
        </section>
      </RequireRole>
    </>
  );
}

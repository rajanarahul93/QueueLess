import { MyAppointments } from "@/components/appointments/MyAppointments";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default function Page() {
  return (
    <>
      <title>My appointments · QueueLess</title>
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <h1 className="text-h1 font-semibold">My appointments</h1>
          <MyAppointments />
        </section>
      </RequireRole>
    </>
  );
}

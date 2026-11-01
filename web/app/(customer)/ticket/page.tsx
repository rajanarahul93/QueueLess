import { TicketList } from "@/components/ticket/TicketList";
import { RequireRole } from "@/lib/auth/RequireRole";

// Resume after a reload: one active ticket opens directly, several are listed. The title sits
// outside the guard, so the tab is named while the session is checked.
export default function Page() {
  return (
    <>
      <title>Your tickets · QueueLess</title>
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <h1 className="text-h1 font-semibold">Your tickets</h1>
          <TicketList />
        </section>
      </RequireRole>
    </>
  );
}

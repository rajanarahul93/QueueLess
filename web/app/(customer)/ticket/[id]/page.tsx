import { LiveTicket, TicketTitle } from "@/components/ticket/LiveTicket";
import { RequireRole } from "@/lib/auth/RequireRole";

// The title sits outside the guard, so the tab is named while the session is checked.
export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <TicketTitle id={id} />
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <LiveTicket id={id} />
        </section>
      </RequireRole>
    </>
  );
}

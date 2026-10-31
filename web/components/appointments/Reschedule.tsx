"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Alert } from "@/components/Alert";
import { appointmentsQuery } from "@/components/appointments/MyAppointments";
import { BookingFlow } from "@/components/booking/BookingFlow";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { errorMessage } from "@/lib/api/errors";
import type { Appointment } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";

/** The appointment to move, from the Upcoming list (there is no GET by id). Kept once found, so
 *  the page stays after the move takes it out of the list. Not found is one neutral answer for a
 *  wrong id, someone else's, a cancelled or an already moved one. */
function useMovable(id: string) {
  const { status, user } = useAuth();
  const upcoming = useQuery(appointmentsQuery("upcoming", status === "signed-in" && user?.role === "customer"));
  const [kept, setKept] = useState<Appointment | null>(null);
  const found = upcoming.data?.items.find((item) => item.id === id) ?? null;
  if (found && found.id !== kept?.id) setKept(found);
  return { appointment: found ?? kept, upcoming };
}

export function RescheduleTitle({ id }: { id: string }) {
  const { appointment } = useMovable(id);
  const name = useQuery({ ...businessQuery(appointment?.business_id ?? ""), enabled: Boolean(appointment) }).data?.name;
  return <title>{name ? `Move appointment at ${name} · QueueLess` : "Move appointment · QueueLess"}</title>;
}

export function Reschedule({ id }: { id: string }) {
  const { appointment, upcoming } = useMovable(id);
  if (appointment) return <BookingFlow businessId={appointment.business_id} reschedule={appointment} />;
  if (upcoming.isPending) return <DelayedSkeleton rows={4} />;
  if (upcoming.isError) return <Alert>{errorMessage(upcoming.error)}</Alert>;
  return (
    <>
      <h1 className="text-h1 font-semibold">This appointment can&apos;t be moved</h1>
      <p className="mt-2 text-body text-muted">It may have been cancelled, moved or started already.</p>
      <Link href="/me" className="mt-6 inline-block min-h-11 py-3 text-primary underline">
        Back to my appointments
      </Link>
    </>
  );
}

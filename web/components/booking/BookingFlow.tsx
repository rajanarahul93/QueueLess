"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { useBusiness, useServices } from "@/components/booking/BusinessDetail";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { DateStrip, MAX_DAYS_AHEAD, SlotGrid } from "@/components/booking/SlotPicker";
import { api, ApiError, newIdempotencyKey } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { Appointment, Availability, Slot } from "@/lib/api/types";
import { addDays, dayLabel, localToday, timeLabel } from "@/lib/time";
import { useCountdown } from "@/lib/useCountdown";

type Body = { service_id: string; resource_id: string; start: string };

/** POST /v1/appointments with this action's key. A duplicate still in progress (the same key
 *  racing itself) is retried once, with the same key, after Retry-After. */
export async function book(body: Body, key: string): Promise<Appointment> {
  const send = () => api<Appointment>("/v1/appointments", { method: "POST", body, idempotencyKey: key });
  try {
    return await send();
  } catch (error) {
    if (!(error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT" && error.body.in_progress)) {
      throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, (error.retryAfterSec ?? 1) * 1000));
    return send();
  }
}

function validDate(value: string | null, today: string): string {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) && value >= today && value <= addDays(today, MAX_DAYS_AHEAD)
    ? value
    : today;
}

export function BookingFlow({ businessId }: { businessId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const business = useBusiness(businessId);
  const services = useServices(businessId);
  const zone = business.data?.timezone;
  const today = zone ? localToday(zone) : null;
  const serviceId = params.get("service") ?? services.data?.[0]?.id ?? null;
  const date = today ? validDate(params.get("date"), today) : null;
  const service = services.data?.find((item) => item.id === serviceId);

  const availability = useQuery({
    queryKey: ["availability", businessId, serviceId, date],
    queryFn: () =>
      api<Availability>(`/v1/businesses/${businessId}/availability?service_id=${serviceId}&date=${date}`),
    enabled: Boolean(serviceId && date),
    staleTime: 0,
    retry: false,
  });

  const [selected, setSelected] = useState<Slot | null>(null);
  const [taken, setTaken] = useState<Slot[] | null>(null); // next_slots after SLOT_TAKEN
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [booked, setBooked] = useState<Appointment | null>(null);
  const [wait, startWait] = useCountdown();
  const inFlight = useRef(false);
  const action = useRef<{ start: string; key: string } | null>(null); // one key per chosen slot
  const takenAlert = useRef<HTMLDivElement>(null);
  const doneHeading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (taken) takenAlert.current?.focus();
  }, [taken]);
  useEffect(() => {
    if (booked) doneHeading.current?.focus();
  }, [booked]);

  function go(next: { service?: string; date?: string }) {
    const query = new URLSearchParams({ service: next.service ?? serviceId ?? "", date: next.date ?? date ?? "" });
    setSelected(null);
    setTaken(null);
    setError(null);
    router.replace(`/b/${businessId}/book?${query}`, { scroll: false });
  }

  function choose(slot: Slot) {
    setSelected(slot);
    setError(null);
  }

  async function confirm() {
    if (inFlight.current || !selected || !serviceId) return; // a second click or Enter does nothing
    inFlight.current = true;
    setBusy(true);
    setError(null);
    if (action.current?.start !== selected.start) {
      action.current = { start: selected.start, key: newIdempotencyKey() };
    }
    const body = { service_id: serviceId, resource_id: selected.resource_id, start: selected.start };
    try {
      const appointment = await book(body, action.current.key);
      action.current = null;
      setBooked(appointment);
      void queryClient.invalidateQueries({ queryKey: ["availability", businessId] });
      void queryClient.invalidateQueries({ queryKey: ["appointments"] });
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "SLOT_TAKEN") {
        const next = Array.isArray(failure.body.next_slots) ? (failure.body.next_slots as Slot[]) : [];
        setSelected(null);
        setTaken(next);
        action.current = null;
        void availability.refetch();
      } else {
        setError(errorMessage(failure));
        if (failure instanceof ApiError && failure.retryAfterSec) startWait(failure.retryAfterSec);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (business.isPending || services.isPending) return <DelayedSkeleton rows={4} />;
  if (business.isError || services.isError) return <Alert>{errorMessage(business.error ?? services.error)}</Alert>;
  if (!zone || !today || !date) return null;

  if (booked) {
    return (
      <div role="status">
        <h1 ref={doneHeading} tabIndex={-1} className="text-h1 font-semibold">
          You&apos;re booked
        </h1>
        <p className="mt-2 text-body-lg">
          {booked.service_name} on {dayLabel(booked.start.slice(0, 10), "long")} at {timeLabel(booked.start, zone)}
        </p>
        <p className="text-caption text-muted">Time local to {business.data.name} ({zone}).</p>
        <div className="mt-6 flex flex-col gap-3">
          <Button variant="outline" onClick={() => { setBooked(null); go({}); }}>
            Book another time
          </Button>
          <Link href={`/b/${businessId}`} className="min-h-11 py-3 text-center text-label text-primary underline">
            Back to {business.data.name}
          </Link>
        </div>
      </div>
    );
  }

  const slots = availability.data?.slots ?? [];
  return (
    <>
      <h1 className="text-h1 font-semibold">Book at {business.data.name}</h1>
      {services.data.length > 1 && (
        <div className="mt-4">
          <label htmlFor="service" className="block text-label font-medium">
            Service
          </label>
          <select id="service" value={serviceId ?? ""} onChange={(event) => go({ service: event.target.value })}
            className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body">
            {services.data.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} ({item.duration_min} min)
              </option>
            ))}
          </select>
        </div>
      )}
      {!service ? (
        <Alert>This service isn&apos;t available to book. Pick another.</Alert>
      ) : (
        <>
          <DateStrip today={today} date={date} onPick={(day) => go({ date: day })} />

          {taken && (
            <div ref={takenAlert} tabIndex={-1} role="alert"
              className="mt-6 rounded-md border border-waiting bg-waiting-bg p-4 text-waiting">
              <p className="text-body font-medium">That time was just taken. Pick another.</p>
              {taken.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {taken.slice(0, 3).map((slot) => (
                    <button key={slot.start} type="button" onClick={() => { setTaken(null); choose(slot); }}
                      className="min-h-11 rounded-md border border-border-strong bg-surface px-3 text-label font-medium text-text tabular-nums">
                      {timeLabel(slot.start, zone)}
                    </button>
                  ))}
                </div>
              ) : (
                <button type="button" onClick={() => go({ date: addDays(date, 1) })}
                  className="mt-2 min-h-11 text-label underline">
                  No more times that day. See the next day
                </button>
              )}
            </div>
          )}

          <div aria-live="polite">
            {availability.isPending && <DelayedSkeleton rows={2} />}
            {availability.isError && <Alert>{errorMessage(availability.error)}</Alert>}
            {availability.isSuccess &&
              (slots.length === 0 ? (
                <div className="mt-6">
                  <p className="text-body text-muted">No free times on this day.</p>
                  {date < addDays(today, MAX_DAYS_AHEAD) && (
                    <button type="button" onClick={() => go({ date: addDays(date, 1) })}
                      className="mt-2 min-h-11 text-label text-primary underline">
                      See the next day
                    </button>
                  )}
                </div>
              ) : (
                <SlotGrid slots={slots} zone={zone} selected={selected?.start ?? null} onSelect={choose} />
              ))}
          </div>
          <p className="mt-3 text-caption text-muted">
            {service.duration_min} min · times shown in {zone}
          </p>

          {selected && (
            <section aria-label="Confirm booking" className="mt-6 rounded-lg border border-border bg-surface p-4 shadow-2">
              <h2 className="text-h2 font-semibold">Confirm</h2>
              <p className="mt-1 text-body">
                {service.name}, {dayLabel(date, "long")} at {timeLabel(selected.start, zone)} ({zone})
              </p>
              {error && <Alert>{error}</Alert>}
              <Button type="button" onClick={() => void confirm()} loading={busy} disabled={wait > 0}
                className="mt-4 w-full">
                {wait > 0 ? `Confirm booking (wait ${wait} s)` : "Confirm booking"}
              </Button>
            </section>
          )}
        </>
      )}
    </>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { newIdempotencyKey, sendKeyed } from "@/lib/api/client";
import type { Token } from "@/lib/api/types";

export const SLOW_PRESS_MS = 5000;

/** What a press produced: whose appointment it was, the token, and whether this press is what
 *  created it (the response body carries no appointment id). */
export type CheckedIn = { appointmentId: string; token: Token; created: boolean };

/** The staff check-in's one door to the API, with 6D-2's press rules:
 *
 *  - one Idempotency-Key per deliberate press, kept while the press is the same, so the 5 s
 *    retry cannot check somebody in twice; a press on another row is another press;
 *  - one request at a time (a guard in code, not only a disabled button), and nothing at all
 *    while a row is held because someone else's change relabelled it;
 *  - nothing is optimistic: the response is written into the cache and the refetch settles
 *    the rest;
 *  - 201 created and 200 already-checked-in are both successes; only a problem is an error.
 *
 *  It is a sibling of `useAction`, not a generalisation of it: that hook speaks the queue's
 *  version-and-conflict language, and this endpoint has no body and no version at all. */
export function useCheckIn(onToken: (result: CheckedIn) => void, blocked = false) {
  const [busy, setBusy] = useState<string | null>(null); // the appointment id in flight
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const stamps = useRef(new Map<string, string>());
  const alive = useRef(true);
  // Read inside run(), never during render, so the guard sees the current value.
  const holding = useRef(blocked);
  useEffect(() => {
    holding.current = blocked;
  }, [blocked]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (busy === null) return;
    const timer = setTimeout(() => setSlow(true), SLOW_PRESS_MS);
    return () => clearTimeout(timer);
  }, [busy]);

  const run = useCallback(
    async (appointmentId: string): Promise<CheckedIn | "failed"> => {
      // Two separate guards: one request at a time, and nothing while a row is held because
      // somebody else's change renamed its action. The disabled attribute is the visible half.
      if (holding.current) return "failed";
      if (inFlight.current && !slow) return "failed"; // a double press sends one request
      const stamp = stamps.current.get(appointmentId) ?? newIdempotencyKey();
      stamps.current.set(appointmentId, stamp);
      inFlight.current = true;
      setBusy(appointmentId);
      setSlow(false);
      setError(null);
      try {
        let created = false; // 201 created it, 200 found the token that was already there
        const token = await sendKeyed<Token>(
          `/v1/appointments/${appointmentId}/check-in`,
          "POST",
          undefined,
          stamp,
          (status) => {
            created = status === 201;
          },
        );
        stamps.current.delete(appointmentId); // that press is over; the next one is new
        const result = { appointmentId, token, created };
        if (alive.current) onToken(result);
        return result;
      } catch (caught) {
        if (alive.current) setError(caught);
        return "failed";
      } finally {
        inFlight.current = false;
        if (alive.current) setBusy(null);
      }
    },
    [onToken, slow],
  );

  return { run, busy, slow, error, clearError: () => setError(null) };
}

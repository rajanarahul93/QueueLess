"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { newIdempotencyKey, sendKeyed } from "@/lib/api/client";
import type { Business } from "@/lib/api/types";

export const SLOW_PRESS_MS = 5000;

/** What the owner filled in, in the shape the API takes. */
export type NewBusiness = {
  name: string;
  category: string;
  address?: string;
  timezone: string;
  open_hours: Record<string, { open: string; close: string }[]>;
};

/** Creating a business, with 6D's press rules:
 *
 *  - one Idempotency-Key per deliberate press, kept while the body stays the same, so the 5 s
 *    retry cannot create a second business; a changed field is a different press and a new key;
 *  - one request at a time (a guard in code, not only a disabled button);
 *  - nothing optimistic: the caller refetches the list rather than trusting the response. */
export function useCreateBusiness() {
  const [busy, setBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const stamps = useRef(new Map<string, string>());
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!busy) return;
    const timer = setTimeout(() => setSlow(true), SLOW_PRESS_MS);
    return () => clearTimeout(timer);
  }, [busy]);

  const run = useCallback(
    async (body: NewBusiness): Promise<Business | "failed"> => {
      if (inFlight.current && !slow) return "failed"; // a double press sends one request
      // The press is identified by what it will send: an edited field is a new press.
      const identity = JSON.stringify(body);
      const stamp = stamps.current.get(identity) ?? newIdempotencyKey();
      stamps.current.set(identity, stamp);
      inFlight.current = true;
      setBusy(true);
      setSlow(false);
      setError(null);
      try {
        const created = await sendKeyed<Business>("/v1/businesses", "POST", body, stamp);
        stamps.current.delete(identity); // that press is over; the next one is new
        return created;
      } catch (caught) {
        if (alive.current) setError(caught);
        return "failed";
      } finally {
        inFlight.current = false;
        if (alive.current) setBusy(false);
      }
    },
    [slow],
  );

  return { run, busy, slow, error };
}

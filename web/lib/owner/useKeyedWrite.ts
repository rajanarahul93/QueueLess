"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { newIdempotencyKey, sendKeyed } from "@/lib/api/client";

export const SLOW_PRESS_MS = 5000;

/** The owner write rules, in one place (6D's press rules, which every owner form follows):
 *
 *  - one Idempotency-Key per deliberate press, kept while the body stays the same, so the 5 s
 *    retry cannot write twice; a changed field is a different press and a new key;
 *  - one request at a time (a guard in code, not only a disabled button);
 *  - nothing optimistic: the caller refetches rather than trusting the response.
 *
 *  Owner writes have no version control, so a caller sends only the fields it changed. */
export function useKeyedWrite<T>() {
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
    async (path: string, method: "POST" | "PATCH", body: object): Promise<T | "failed"> => {
      if (inFlight.current && !slow) return "failed"; // a double press sends one request
      // The press is identified by where it writes and what it will send.
      const identity = `${method} ${path} ${JSON.stringify(body)}`;
      const stamp = stamps.current.get(identity) ?? newIdempotencyKey();
      stamps.current.set(identity, stamp);
      inFlight.current = true;
      setBusy(true);
      setSlow(false);
      setError(null);
      try {
        const answer = await sendKeyed<T>(path, method, body, stamp);
        stamps.current.delete(identity); // that press is over; the next one is new
        return answer;
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

  const forget = useCallback(() => setError(null), []);
  return { run, busy, slow, error, forget };
}

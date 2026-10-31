"use client";

import { useCallback, useEffect, useState } from "react";

/** Seconds left until a deadline; start(seconds) restarts it. Ticks once a second. */
export function useCountdown(): [number, (seconds: number) => void] {
  const [deadline, setDeadline] = useState(0);
  const [left, setLeft] = useState(0);

  useEffect(() => {
    if (!deadline) return;
    const tick = () => setLeft(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [deadline]);

  const start = useCallback((seconds: number) => setDeadline(Date.now() + seconds * 1000), []);
  return [left, start];
}

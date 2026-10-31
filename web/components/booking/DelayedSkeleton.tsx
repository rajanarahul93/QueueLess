"use client";

import { useEffect, useState } from "react";

/** Skeleton rows that appear only if loading takes over 400 ms (faster loads show nothing). */
export function DelayedSkeleton({ rows }: { rows: number }) {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setShown(true), 400);
    return () => clearTimeout(timer);
  }, []);
  if (!shown) return null;
  return (
    <div aria-busy="true" aria-label="Loading" className="space-y-2">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-16 rounded-md bg-border" />
      ))}
    </div>
  );
}

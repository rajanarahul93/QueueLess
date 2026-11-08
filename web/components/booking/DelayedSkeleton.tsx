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
    // role="status": a plain div is `generic`, which may not carry a label at all (axe's
    // aria-prohibited-attr), and a loading indicator is exactly what status is for.
    <div role="status" aria-busy="true" aria-label="Loading" className="space-y-2">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-16 rounded-md bg-border" />
      ))}
    </div>
  );
}

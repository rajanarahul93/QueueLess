/** A thin site-wide notice on the public demo. Shown only when the build sets
 *  NEXT_PUBLIC_DEMO_MODE=true (inlined at build time); every other build renders nothing. */
export function DemoBanner() {
  if (process.env.NEXT_PUBLIC_DEMO_MODE !== "true") return null;
  return (
    <p className="border-b border-border-strong bg-surface px-4 py-1 text-center text-label text-muted">
      Demo environment: data may be reset
    </p>
  );
}

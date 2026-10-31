/** Placeholder blocks the size of a typical screen (no layout shift); static, no shimmer. */
export function PageSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading" className="mx-auto max-w-[480px] space-y-4 px-4 py-8">
      <div className="h-8 w-2/3 rounded-sm bg-border" />
      <div className="h-32 rounded-md bg-border" />
      <div className="h-12 rounded-md bg-border" />
    </div>
  );
}

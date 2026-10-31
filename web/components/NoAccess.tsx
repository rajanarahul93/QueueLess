import Link from "next/link";

export function NoAccess() {
  return (
    <section className="mx-auto max-w-[480px] px-4 py-12 text-center">
      <h1 className="text-h1 font-semibold">You don&apos;t have access to this page</h1>
      <p className="mt-2 text-body text-muted">This part of QueueLess is for another kind of account.</p>
      <Link href="/" className="mt-6 inline-block min-h-11 py-3 text-primary underline">
        Go to the home page
      </Link>
    </section>
  );
}

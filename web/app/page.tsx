import Link from "next/link";

export default function Home() {
  return (
    <section className="mx-auto max-w-[480px] px-4 py-12">
      <h1 className="text-h1 font-semibold">QueueLess</h1>
      <p className="mt-2 text-body-lg text-muted">Book a slot or join the queue, and see your turn live.</p>
      <nav className="mt-8 flex flex-col gap-3">
        <Link href="/b" className="flex min-h-12 items-center justify-center rounded-md bg-primary px-4 text-label font-medium text-primary-fg">
          Find a business
        </Link>
        <Link href="/login" className="flex min-h-12 items-center justify-center rounded-md border border-border-strong px-4 text-label font-medium">
          Sign in with your phone
        </Link>
        <Link href="/signin" className="flex min-h-12 items-center justify-center rounded-md border border-border-strong px-4 text-label font-medium">
          Staff and owner sign in
        </Link>
      </nav>
    </section>
  );
}

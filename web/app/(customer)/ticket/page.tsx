import Link from "next/link";
import { RequireRole } from "@/lib/auth/RequireRole";

// The live ticket arrives in the next update; checked-in appointments point here already.
export default function Page() {
  return (
    <>
      <title>Your ticket · QueueLess</title>
      <RequireRole roles={["customer"]} signInPath="/login">
        <section className="mx-auto max-w-[480px] px-4 py-8">
          <h1 className="text-h1 font-semibold">Your live ticket</h1>
          <p className="mt-2 text-body text-muted">
            The live ticket screen arrives in the next update. Until then, staff at the business will call you.
          </p>
          <Link href="/me" className="mt-6 inline-block min-h-11 py-3 text-primary underline">
            Back to my appointments
          </Link>
        </section>
      </RequireRole>
    </>
  );
}

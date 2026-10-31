import { Suspense } from "react";
import { BusinessSearch } from "@/components/booking/BusinessSearch";

// The shell renders on the server; the list is fetched by the browser, so the listing's
// per-IP limit counts each visitor rather than the web server.
export default function SearchPage() {
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <title>Find a business · QueueLess</title>
      <h1 className="text-h1 font-semibold">Find a business</h1>
      <Suspense>
        <BusinessSearch />
      </Suspense>
    </section>
  );
}

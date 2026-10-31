"use client";

import { useAuth } from "@/lib/auth/AuthProvider";

export default function Page() {
  const { user } = useAuth();
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Signed in as {user?.name}</h1>
      <p className="mt-2 text-body text-muted">Business setup and reports arrive in a later update.</p>
    </section>
  );
}

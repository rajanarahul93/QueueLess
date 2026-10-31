"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { Form } from "@/components/Form";
import { errorMessage } from "@/lib/api/errors";
import { homeFor, safeNext, signIn } from "@/lib/auth/actions";

function EmailSignIn() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const session = await signIn(String(form.get("email")), String(form.get("password")));
      router.replace(next ?? homeFor(session.user.role));
    } catch (failure) {
      setError(errorMessage(failure));
      setBusy(false);
    }
  }

  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Staff and owner sign in</h1>
      <Form onSubmit={onSubmit} className="mt-6 space-y-4">
        <TextField id="email" name="email" label="Email" type="email" autoComplete="email" required />
        <TextField
          id="password"
          name="password"
          label="Password"
          type="password"
          autoComplete="current-password"
          required
        />
        {error && <Alert>{error}</Alert>}
        <Button type="submit" loading={busy} className="w-full">
          Sign in
        </Button>
      </Form>
      <p className="mt-6 text-body text-muted">
        New business?{" "}
        <Link href="/register" className="text-primary underline">
          Create an owner account
        </Link>
      </p>
    </section>
  );
}

export default function SignInPage() {
  return (
    <>
      <title>Staff and owner sign in · QueueLess</title>
      <Suspense>
        <EmailSignIn />
      </Suspense>
    </>
  );
}

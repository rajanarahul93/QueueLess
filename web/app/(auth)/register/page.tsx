"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { TextField } from "@/components/TextField";
import { errorMessage, invalidFields } from "@/lib/api/errors";
import { registerOwner } from "@/lib/auth/actions";

const MIN_PASSWORD = 10; // the API's rule

export default function RegisterPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password"));
    if (password.length < MIN_PASSWORD) {
      setFields(["password"]);
      setError(null);
      return;
    }
    setBusy(true);
    setError(null);
    setFields([]);
    try {
      await registerOwner(String(form.get("name")), String(form.get("email")), password);
      router.replace("/owner");
    } catch (failure) {
      setFields(invalidFields(failure));
      setError(errorMessage(failure));
      setBusy(false);
    }
  }

  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Create an owner account</h1>
      <form onSubmit={onSubmit} className="mt-6 space-y-4">
        <TextField id="name" name="name" label="Your name" autoComplete="name" required maxLength={100} />
        <TextField
          id="email"
          name="email"
          label="Email"
          type="email"
          autoComplete="email"
          required
          error={fields.includes("email") ? "Use another email address." : undefined}
        />
        <TextField
          id="password"
          name="password"
          label={`Password (at least ${MIN_PASSWORD} characters)`}
          type="password"
          autoComplete="new-password"
          required
          maxLength={128}
          error={fields.includes("password") ? `Use at least ${MIN_PASSWORD} characters.` : undefined}
        />
        {error && <Alert>{error}</Alert>}
        <Button type="submit" loading={busy} className="w-full">
          Create account
        </Button>
      </form>
    </section>
  );
}

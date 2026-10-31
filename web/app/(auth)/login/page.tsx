"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { OtpInput } from "@/components/OtpInput";
import { ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import { homeFor, requestOtp, safeNext, verifyOtp } from "@/lib/auth/actions";
import { COUNTRY_CODES, toE164 } from "@/lib/phone";
import { useCountdown } from "@/lib/useCountdown";

const RESEND_SEC = 30;

function PhoneLogin() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [country, setCountry] = useState<string>(COUNTRY_CODES[0].code);
  const [number, setNumber] = useState("");
  const [phone, setPhone] = useState<string | null>(null); // set once a code was sent
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [wait, startWait] = useCountdown();

  async function send(target: string) {
    setBusy(true);
    setError(null);
    try {
      await requestOtp(target);
      setPhone(target);
      setCode("");
      startWait(RESEND_SEC);
    } catch (failure) {
      setError(errorMessage(failure));
      if (failure instanceof ApiError && failure.retryAfterSec) startWait(failure.retryAfterSec);
    } finally {
      setBusy(false);
    }
  }

  function onSend(event: FormEvent) {
    event.preventDefault();
    const target = toE164(country, number);
    if (!target) {
      setError("Enter a valid mobile number.");
      return;
    }
    void send(target);
  }

  async function onVerify(entered: string) {
    if (!phone) return;
    setBusy(true);
    setError(null);
    try {
      const session = await verifyOtp(phone, entered);
      router.replace(next ?? homeFor(session.user.role));
    } catch (failure) {
      setError(errorMessage(failure));
      setCode("");
      if (failure instanceof ApiError && failure.retryAfterSec) startWait(failure.retryAfterSec);
      setBusy(false);
    }
  }

  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Sign in with your phone</h1>
      {!phone ? (
        <form onSubmit={onSend} noValidate className="mt-6 space-y-4">
          <div>
            <label htmlFor="country" className="block text-label font-medium">
              Country code
            </label>
            <select
              id="country"
              value={country}
              onChange={(event) => setCountry(event.target.value)}
              className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body"
            >
              {COUNTRY_CODES.map((option) => (
                <option key={option.code} value={option.code}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="phone" className="block text-label font-medium">
              Mobile number
            </label>
            <input
              id="phone"
              type="tel"
              inputMode="tel"
              autoComplete="tel-national"
              value={number}
              onChange={(event) => setNumber(event.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "login-error" : undefined}
              className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body"
            />
          </div>
          {error && <Alert id="login-error">{error}</Alert>}
          <Button type="submit" loading={busy} disabled={wait > 0} className="w-full">
            {wait > 0 ? `Send code (wait ${wait} s)` : "Send code"}
          </Button>
        </form>
      ) : (
        <div className="mt-6 space-y-4">
          <p className="text-body text-muted">
            Enter the 6-digit code we sent to {phone.slice(0, -4).replace(/\d/g, "•")}
            {phone.slice(-4)}.
          </p>
          <OtpInput
            value={code}
            onChange={setCode}
            onComplete={(entered) => void onVerify(entered)}
            invalid={Boolean(error)}
            describedBy={error ? "otp-error" : undefined}
            disabled={busy}
          />
          {error && <Alert id="otp-error">{error}</Alert>}
          <div className="flex flex-wrap items-center gap-4">
            <button
              type="button"
              disabled={wait > 0 || busy}
              onClick={() => void send(phone)}
              className="min-h-11 text-label text-primary underline disabled:text-muted disabled:no-underline"
            >
              {wait > 0 ? `Resend in ${wait} s` : "Resend code"}
            </button>
            <button
              type="button"
              onClick={() => {
                setPhone(null);
                setError(null);
              }}
              className="min-h-11 text-label text-primary underline"
            >
              Use another number
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

export default function LoginPage() {
  return (
    <>
      <title>Sign in with your phone · QueueLess</title>
      <Suspense>
        <PhoneLogin />
      </Suspense>
    </>
  );
}

import { api } from "@/lib/api/client";
import { adopt, type Session, type User } from "@/lib/auth/session";

type AuthBody = { access_token: string; user: User };

function start(body: AuthBody): Session {
  const session = { accessToken: body.access_token, user: body.user };
  adopt(session);
  return session;
}

export function requestOtp(phone: string): Promise<void> {
  return api("/v1/auth/otp/request", { method: "POST", body: { phone }, auth: false });
}

export async function verifyOtp(phone: string, code: string): Promise<Session> {
  return start(
    await api<AuthBody>("/v1/auth/otp/verify", {
      method: "POST",
      body: { phone, code },
      auth: false,
    }),
  );
}

export async function signIn(email: string, password: string): Promise<Session> {
  return start(
    await api<AuthBody>("/v1/auth/login", { method: "POST", body: { email, password }, auth: false }),
  );
}

/** Owner accounts only (staff are created by their owner); signs the new owner in. */
export async function registerOwner(name: string, email: string, password: string) {
  await api("/v1/auth/register", { method: "POST", body: { name, email, password }, auth: false });
  return signIn(email, password);
}

/** Where a signed-in user lands. Admin has no screens in v1. */
export function homeFor(role: string): string {
  return { customer: "/me", staff: "/console", owner: "/owner" }[role] ?? "/no-access";
}

/** `next` from the URL, only when it is a path on this site (no open redirect). */
export function safeNext(next: string | null): string | null {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\")
    ? next
    : null;
}

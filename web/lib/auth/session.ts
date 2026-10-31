import { API_URL } from "@/lib/api/config";
import type { components } from "@/lib/api/schema";
import { abortAllStreams } from "@/lib/stream/registry";

export type User = components["schemas"]["UserOut"];
export type Session = { accessToken: string; user: User };
type Message = { type: "session"; session: Session } | { type: "logout" };

// The access token lives in this module only: never in localStorage, sessionStorage or a cookie
// the page can read. A reload starts empty and refreshes from the HttpOnly cookie.
let session: Session | null = null;
let generation = 0; // bumps on every sign-in, refresh, adoption and logout
let inflight: Promise<Session | null> | null = null;
const listeners = new Set<(session: Session | null) => void>();
const channel =
  typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("queueless-auth");

channel?.addEventListener("message", (event: MessageEvent<Message>) => {
  if (event.data.type === "session") adopt(event.data.session, false);
  else endSession(false);
});

export function getAccessToken(): string | null {
  return session?.accessToken ?? null;
}

export function getSession(): Session | null {
  return session;
}

export function onSessionChange(listener: (session: Session | null) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Starts a session (sign-in or refresh) and tells the other tabs. */
export function adopt(next: Session, announce = true): void {
  session = next;
  generation += 1;
  listeners.forEach((listener) => listener(next));
  if (announce) channel?.postMessage({ type: "session", session: next } satisfies Message);
}

function endSession(announce: boolean): void {
  session = null;
  generation += 1;
  abortAllStreams();
  listeners.forEach((listener) => listener(null));
  if (announce) channel?.postMessage({ type: "logout" } satisfies Message);
}

/** One refresh at a time: callers in this tab share the in-flight promise, and tabs take turns
 *  through a Web Lock. Refresh tokens rotate, and a second use of a rotated one logs the user
 *  out everywhere, so two refreshes must never race. A tab that waited for the lock while
 *  another tab refreshed adopts that tab's session instead of refreshing again. */
export function refreshSession(): Promise<Session | null> {
  if (!inflight) {
    const seen = generation;
    inflight = withLock(() =>
      generation !== seen ? Promise.resolve(session) : callRefresh(),
    ).finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

function withLock<T>(task: () => Promise<T>): Promise<T> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  return locks ? locks.request("queueless-auth-refresh", task) : task();
}

async function callRefresh(): Promise<Session | null> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/v1/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  } catch {
    return null; // offline: keep what we have, the caller reports the failure
  }
  if (!response.ok) {
    if (session) endSession(false); // the cookie is shared, so every tab finds out itself
    return null;
  }
  const body = (await response.json()) as { access_token: string; user: User };
  const next = { accessToken: body.access_token, user: body.user };
  adopt(next);
  return next;
}

/** Ends the session here and in every other tab; the server revokes the refresh family. */
export async function logout(): Promise<void> {
  await fetch(`${API_URL}/v1/auth/logout`, { method: "POST", credentials: "include" }).catch(
    () => undefined, // signed out locally either way
  );
  endSession(true);
}

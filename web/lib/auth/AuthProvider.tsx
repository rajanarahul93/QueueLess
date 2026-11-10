"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { getSession, lastRefreshOutcome, onSessionChange, refreshSession, type User } from "@/lib/auth/session";

type Auth = {
  status: "loading" | "signed-in" | "signed-out" | "unreachable";
  user: User | null;
  /** Try the silent refresh again, for the one screen that offers it. */
  retry: () => void;
};

/** A refresh that never reached the server is retried on this ladder before the screen says so.
 *  The server forgives a rotated token for `REFRESH_REUSE_GRACE_SEC` (10 s), so a response lost
 *  after the cookie was spent is still recoverable inside this window. */
const BACKOFF_MS = [1000, 2000, 4000];

const AuthContext = createContext<Auth>({ status: "loading", user: null, retry: () => undefined });

export function useAuth(): Auth {
  return useContext(AuthContext);
}

/** Restores the session from the refresh cookie on load (one silent refresh) and follows every
 *  later change, including sign-in and logout in another tab. A refusal signs out; a request
 *  that never got an answer does not, because the cookie was never spent. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [auth, setAuth] = useState<Omit<Auth, "retry">>(() => {
    const session = getSession();
    return session ? { status: "signed-in", user: session.user } : { status: "loading", user: null };
  });
  const alive = useRef(true);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const restore = useCallback(async function attempt(step = 0): Promise<void> {
    if (!alive.current || getSession()) return;
    const session = await refreshSession();
    if (!alive.current || session) return; // adopted meanwhile, or this tab is gone
    if (lastRefreshOutcome() !== "unreachable") {
      setAuth({ status: "signed-out", user: null }); // the server refused: really signed out
      return;
    }
    if (step < BACKOFF_MS.length) {
      timers.current.push(setTimeout(() => void attempt(step + 1), BACKOFF_MS[step]));
      return;
    }
    setAuth({ status: "unreachable", user: null });
  }, []);

  const retry = useCallback(() => {
    setAuth({ status: "loading", user: null });
    void restore();
  }, [restore]);

  useEffect(() => {
    alive.current = true;
    const stop = onSessionChange((session) => {
      if (!session) queryClient.clear(); // nothing cached outlives the session
      setAuth(session ? { status: "signed-in", user: session.user } : { status: "signed-out", user: null });
    });
    if (!getSession()) void restore();
    return () => {
      alive.current = false;
      timers.current.forEach(clearTimeout);
      timers.current = [];
      stop();
    };
  }, [queryClient, restore]);

  return <AuthContext.Provider value={{ ...auth, retry }}>{children}</AuthContext.Provider>;
}

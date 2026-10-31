"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getSession, onSessionChange, refreshSession, type User } from "@/lib/auth/session";

type Auth = { status: "loading" | "signed-in" | "signed-out"; user: User | null };

const AuthContext = createContext<Auth>({ status: "loading", user: null });

export function useAuth(): Auth {
  return useContext(AuthContext);
}

/** Restores the session from the refresh cookie on load (one silent refresh) and follows every
 *  later change, including sign-in and logout in another tab. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [auth, setAuth] = useState<Auth>(() => {
    const session = getSession();
    return session ? { status: "signed-in", user: session.user } : { status: "loading", user: null };
  });

  useEffect(() => {
    const stop = onSessionChange((session) => {
      if (!session) queryClient.clear(); // nothing cached outlives the session
      setAuth(session ? { status: "signed-in", user: session.user } : { status: "signed-out", user: null });
    });
    if (!getSession()) {
      void refreshSession().then((session) => {
        if (!session) setAuth({ status: "signed-out", user: null });
      });
    }
    return stop;
  }, [queryClient]);

  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}

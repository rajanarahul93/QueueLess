"use client";

import Link from "next/link";
import { useEffect, useSyncExternalStore } from "react";
import { useAuth } from "@/lib/auth/AuthProvider";
import { logout } from "@/lib/auth/session";

type Theme = "system" | "light" | "dark";
const THEME_KEY = "queueless-theme"; // a display preference, not a secret
const NEXT: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

function readTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.classList.toggle("light", theme === "light");
}

const themeListeners = new Set<() => void>();

function subscribeTheme(listener: () => void) {
  themeListeners.add(listener);
  return () => themeListeners.delete(listener);
}

function saveTheme(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    // storage blocked: the choice is not kept
  }
  themeListeners.forEach((listener) => listener());
}

export function Header() {
  const { status, user } = useAuth();
  const theme = useSyncExternalStore(subscribeTheme, readTheme, () => "system" as Theme);

  useEffect(() => applyTheme(theme), [theme]);

  return (
    <header className="border-b border-border bg-surface">
      <div className="mx-auto flex max-w-5xl items-center justify-between gap-2 px-4 sm:px-6">
        <Link href="/" className="flex min-h-11 items-center text-h2 font-semibold">
          QueueLess
        </Link>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => saveTheme(NEXT[theme])} className="min-h-11 rounded-md px-3 text-label">
            Theme: {theme === "system" ? "System" : theme === "light" ? "Light" : "Dark"}
          </button>
          {status === "signed-in" && user && (
            <button
              type="button"
              onClick={() => void logout()}
              className="min-h-11 rounded-md border border-border-strong px-3 text-label"
            >
              Sign out
            </button>
          )}
        </div>
      </div>
    </header>
  );
}

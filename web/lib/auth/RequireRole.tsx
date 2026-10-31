"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { NoAccess } from "@/components/NoAccess";
import { PageSkeleton } from "@/components/PageSkeleton";
import { useAuth } from "@/lib/auth/AuthProvider";

/** Renders children only for the given roles. Signed out: off to `signInPath`, coming back
 *  afterwards. Signed in with another role (admin included): the no-access screen. The API
 *  checks every call anyway; this keeps people out of screens that cannot work for them. */
export function RequireRole({
  roles,
  signInPath,
  children,
}: {
  roles: readonly string[];
  signInPath: string;
  children: ReactNode;
}) {
  const { status, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (status === "signed-out") {
      // The query string too, so a Book page comes back with its service and date.
      const here = `${pathname}${window.location.search}`;
      router.replace(`${signInPath}?next=${encodeURIComponent(here)}`);
    }
  }, [status, router, signInPath, pathname]);

  if (status !== "signed-in" || !user) return <PageSkeleton />;
  if (!roles.includes(user.role)) return <NoAccess />;
  return <>{children}</>;
}

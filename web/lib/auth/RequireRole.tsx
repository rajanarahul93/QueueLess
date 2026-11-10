"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { Button } from "@/components/Button";
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
  const { status, user, retry } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (status === "signed-out") {
      // The query string too, so a Book page comes back with its service and date.
      const here = `${pathname}${window.location.search}`;
      router.replace(`${signInPath}?next=${encodeURIComponent(here)}`);
    }
  }, [status, router, signInPath, pathname]);

  // Unreachable is not signed out: the session may well be fine, so nobody is sent to sign in.
  if (status === "unreachable") {
    return (
      <section className="mx-auto max-w-[480px] px-4 py-12 text-center">
        <p className="text-body">We can&apos;t reach QueueLess. Check your connection.</p>
        <Button type="button" className="mt-4" onClick={retry}>
          Retry
        </Button>
      </section>
    );
  }
  if (status !== "signed-in" || !user) return <PageSkeleton />;
  if (!roles.includes(user.role)) return <NoAccess />;
  return <>{children}</>;
}

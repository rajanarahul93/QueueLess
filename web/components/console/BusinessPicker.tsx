"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Alert } from "@/components/Alert";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { MyBusiness } from "@/lib/api/types";
import { rememberedBusiness } from "@/lib/console/remember";

export const myBusinessesQuery = {
  queryKey: ["businesses", "mine"] as const,
  queryFn: () => api<MyBusiness[]>("/v1/businesses/mine"),
  staleTime: 30_000,
};

const ROLE_TEXT: Record<string, string> = { owner: "Owner", staff: "Staff" };

/** Which business this console is for. One: straight in. Several: a list, with the last one
 *  opened remembered for this tab (ignored unless it is still in the list). */
export function BusinessPicker() {
  const router = useRouter();
  const mine = useQuery({ ...myBusinessesQuery, retry: false });
  const businesses = mine.data;
  const remembered = businesses?.some((business) => business.id === rememberedBusiness()) ? rememberedBusiness() : null;
  const only = businesses?.length === 1 ? businesses[0].id : null;
  const target = only ?? remembered;

  useEffect(() => {
    if (target) router.replace(`/console/${target}`);
  }, [target, router]);

  if (mine.isError) return <Alert>{errorMessage(mine.error)}</Alert>;
  if (mine.isPending || target) return <DelayedSkeleton rows={2} />;
  if (!businesses?.length) {
    return (
      <p className="mt-2 text-body text-muted">
        You are not a member of any business yet. Ask an owner to add you.
      </p>
    );
  }
  return (
    <ul className="mt-4 space-y-3">
      {businesses.map((business) => (
        <li key={business.id}>
          <Link
            href={`/console/${business.id}`}
            className="flex min-h-12 items-center justify-between gap-3 rounded-md border border-border bg-surface p-4"
          >
            <span className="text-body-lg font-semibold">{business.name}</span>
            <span className="text-label text-muted">{ROLE_TEXT[business.role] ?? business.role}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

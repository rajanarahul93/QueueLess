"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Alert } from "@/components/Alert";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { ActiveToken } from "@/lib/api/types";

const STATUS_TEXT: Record<string, string> = { waiting: "Waiting", called: "Called", in_service: "In service" };

/** Resume after a reload: the customer's active tickets. One opens directly; several are listed. */
export function TicketList() {
  const router = useRouter();
  const active = useQuery({
    queryKey: ["tokens", "active"],
    queryFn: () => api<ActiveToken[]>("/v1/tokens?active=true"),
    staleTime: 0,
    retry: false,
  });
  const tickets = active.data ?? [];
  const businesses = useQueries({ queries: [...new Set(tickets.map((item) => item.business_id))].map(businessQuery) });
  const names = new Map(businesses.flatMap((business) => (business.data ? [[business.data.id, business.data.name]] : [])));
  const only = tickets.length === 1 ? tickets[0].id : null;
  useEffect(() => {
    if (only) router.replace(`/ticket/${only}`);
  }, [only, router]);

  if (active.isError) return <Alert>{errorMessage(active.error)}</Alert>;
  if (active.isPending || only) return <DelayedSkeleton rows={2} />;
  if (!tickets.length) {
    return (
      <p className="mt-2 text-body text-muted">
        You have no active tickets.{" "}
        <Link href="/me" className="text-primary underline">
          My appointments
        </Link>
      </p>
    );
  }
  return (
    <ul className="mt-4 space-y-3">
      {tickets.map((item) => (
        <li key={item.id}>
          <Link href={`/ticket/${item.id}`} className="flex min-h-12 items-center justify-between gap-3 rounded-md border border-border bg-surface p-4">
            <span className="text-body-lg font-semibold">
              #{item.token_no} at {names.get(item.business_id) ?? "…"}
            </span>
            <span className="text-label text-muted">{STATUS_TEXT[item.status] ?? item.status}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

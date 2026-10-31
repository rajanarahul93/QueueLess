"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Alert } from "@/components/Alert";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { Business, Service } from "@/lib/api/types";

const DAYS = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
] as const;

export function useBusiness(id: string) {
  return useQuery({
    queryKey: ["business", id],
    queryFn: () => api<Business>(`/v1/businesses/${id}`, { auth: false }),
    staleTime: 60_000,
    retry: false,
  });
}

export function useServices(id: string) {
  return useQuery({
    queryKey: ["services", id],
    queryFn: () => api<Service[]>(`/v1/businesses/${id}/services`, { auth: false }),
    staleTime: 60_000,
    retry: false,
  });
}

export function BusinessDetail({ id }: { id: string }) {
  const business = useBusiness(id);
  const services = useServices(id);
  if (business.isPending || services.isPending) return <DelayedSkeleton rows={4} />;
  if (business.isError || services.isError) {
    return <Alert>{errorMessage(business.error ?? services.error)}</Alert>;
  }
  const { name, address, category, open_hours: hours, timezone } = business.data;
  return (
    <>
      <h1 className="text-h1 font-semibold">{name}</h1>
      <p className="mt-1 text-body text-muted">
        {category}
        {address ? ` · ${address}` : ""}
      </p>

      <h2 className="mt-8 text-h2 font-semibold">Services</h2>
      {services.data.length === 0 ? (
        <p className="mt-2 text-body text-muted">This business has no services to book yet.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {services.data.map((service) => (
            <li key={service.id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface p-4">
              <span>
                <span className="block text-body-lg font-semibold">{service.name}</span>
                <span className="block text-label text-muted">
                  {service.duration_min} min · {service.price}
                </span>
              </span>
              <Link href={`/b/${id}/book?service=${service.id}`}
                aria-label={`Book ${service.name}`}
                className="flex min-h-12 shrink-0 items-center rounded-md bg-primary px-4 text-label font-medium text-primary-fg">
                Book
              </Link>
            </li>
          ))}
        </ul>
      )}

      <h2 className="mt-8 text-h2 font-semibold">Opening hours</h2>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-body">
        {DAYS.map(([key, label]) => (
          <div key={key} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="tabular-nums">
              {hours[key]?.length ? hours[key].map((span) => `${span.open}–${span.close}`).join(", ") : "Closed"}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-caption text-muted">Times are local to the business ({timezone}).</p>
    </>
  );
}

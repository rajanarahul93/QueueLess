"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { errorMessage } from "@/lib/api/errors";
import { servicesQuery } from "@/lib/owner/services";
import {
  byDay,
  byHour,
  clockHour,
  minutes,
  noShowsQuery,
  peakHoursQuery,
  percent,
  range,
  summary,
  waitTimesQuery,
  type NoShow,
  type PeakHour,
  type WaitTime,
} from "@/lib/owner/reports";

/** Bars drawn by hand: a chart library would be a dependency on the one route group with the
 *  tightest JavaScript budget, and these are three small pictures. Every chart carries the same
 *  numbers as a table, so nothing here is only visible in colour or only in a shape. */
function Bars({
  title,
  labels,
  values,
  unit,
}: {
  title: string;
  labels: string[];
  values: number[];
  unit: string;
}) {
  const top = Math.max(1, ...values);
  const width = Math.max(labels.length * 22, 220);
  return (
    <>
      <svg
        viewBox={`0 0 ${width} 120`}
        className="mt-2 h-[120px] w-full"
        role="img"
        aria-label={`${title}. The same numbers are in the table below.`}
        preserveAspectRatio="none"
      >
        {values.map((value, index) => {
          const height = Math.round((value / top) * 100);
          return (
            <rect
              key={labels[index]}
              x={index * 22 + 4}
              y={110 - height}
              width={14}
              height={Math.max(height, value > 0 ? 2 : 0)}
              rx={2}
              fill="var(--primary)"
            />
          );
        })}
        <line x1={0} y1={110} x2={width} y2={110} stroke="var(--border-strong)" strokeWidth={1} />
      </svg>
      <details className="mt-2">
        <summary className="min-h-11 cursor-pointer py-2 text-label text-primary">View data</summary>
        <table className="mt-1 w-full text-label">
          <caption className="text-left text-label text-muted">{title}</caption>
          <thead>
            <tr>
              <th scope="col" className="py-1 text-left font-medium">
                {unit}
              </th>
              <th scope="col" className="py-1 text-right font-medium">
                {title}
              </th>
            </tr>
          </thead>
          <tbody>
            {labels.map((label, index) => (
              <tr key={label} className="border-t border-border">
                <th scope="row" className="py-1 text-left font-normal">
                  {label}
                </th>
                <td className="py-1 text-right">{values[index]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}

export function OwnerDashboard({ businessId }: { businessId: string }) {
  const business = useQuery({ ...businessQuery(businessId), retry: false });
  const zone = business.data?.timezone;
  const span = zone ? range(zone) : null;
  const [peaks, waits, misses] = useQueries({
    queries: [
      { ...peakHoursQuery(businessId, span ?? { from: "", to: "" }), enabled: Boolean(span), retry: false },
      { ...waitTimesQuery(businessId, span ?? { from: "", to: "" }), enabled: Boolean(span), retry: false },
      { ...noShowsQuery(businessId, span ?? { from: "", to: "" }), enabled: Boolean(span), retry: false },
    ],
  });

  // Names only: the table still reads by id when the list cannot be had, so this never fails the page.
  const services = useQuery({ ...servicesQuery(businessId), retry: false });
  const names = new Map((Array.isArray(services.data) ? services.data : []).map((service) => [service.id, service.name]));

  const failed = [business, peaks, waits, misses].find((report) => report.isError);
  if (failed) {
    return (
      <section className="mx-auto max-w-[760px] px-4 py-8">
        <h1 className="text-h1 font-semibold">Dashboard</h1>
        <Alert>{errorMessage(failed.error)}</Alert>
        <Button
          type="button"
          className="mt-4"
          onClick={() => {
            void business.refetch();
            void peaks.refetch();
            void waits.refetch();
            void misses.refetch();
          }}
        >
          Retry
        </Button>
      </section>
    );
  }

  const ready = peaks.data && waits.data && misses.data && span;
  const totals = ready ? summary(peaks.data as PeakHour[], waits.data as WaitTime[], misses.data as NoShow[]) : null;
  const days = ready ? byDay(waits.data as WaitTime[]) : [];
  const hours = ready ? byHour(peaks.data as PeakHour[]) : [];
  const nothing = Boolean(totals && totals.joined === 0 && totals.served === 0 && totals.busiestTokens === 0);

  return (
    <section className="mx-auto max-w-[760px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Dashboard</h1>
      <p className="mt-1 text-label text-muted">
        {span ? `Up to yesterday · ${span.from} to ${span.to}` : "Up to yesterday"}
      </p>

      {!ready ? (
        <DelayedSkeleton rows={4} />
      ) : nothing ? (
        <p className="mt-6 text-body text-muted">
          No data yet. Once people have joined the queue and been served, their days show up here the morning after.
        </p>
      ) : (
        <>
          <ul className="mt-4 grid gap-3 sm:grid-cols-3">
            <li className="rounded-md border border-border bg-surface p-4">
              <p className="text-label text-muted">Busiest hour</p>
              <p className="text-body-lg font-semibold">
                {totals!.busiestHour === null ? "—" : clockHour(totals!.busiestHour)}
              </p>
              <p className="text-label text-muted">{`${totals!.busiestTokens} joined`}</p>
            </li>
            <li className="rounded-md border border-border bg-surface p-4">
              <p className="text-label text-muted">Average wait</p>
              <p className="text-body-lg font-semibold">{`${minutes(totals!.avgWaitSec)} min`}</p>
              <p className="text-label text-muted">{`over ${totals!.served} served`}</p>
            </li>
            <li className="rounded-md border border-border bg-surface p-4">
              <p className="text-label text-muted">No-shows</p>
              <p className="text-body-lg font-semibold">{percent(totals!.noShowRate)}</p>
              <p className="text-label text-muted">{`${totals!.noShows} of ${totals!.joined} joined`}</p>
            </li>
          </ul>

          <h2 className="mt-8 text-h2 font-semibold">When people join</h2>
          <Bars title="Tokens joined" unit="Hour" labels={hours.map((hour) => clockHour(hour.hour))} values={hours.map((hour) => hour.tokens)} />

          <h2 className="mt-8 text-h2 font-semibold">How long they waited</h2>
          <Bars title="Average wait (minutes)" unit="Day" labels={days.map((day) => day.date)} values={days.map((day) => minutes(day.avg))} />
          <details className="mt-2">
            <summary className="min-h-11 cursor-pointer py-2 text-label text-primary">View the slowest tenth too</summary>
            <table className="mt-1 w-full text-label">
              <caption className="text-left text-label text-muted">p90 wait (minutes), the slowest tenth of each day</caption>
              <thead>
                <tr>
                  <th scope="col" className="py-1 text-left font-medium">Day</th>
                  <th scope="col" className="py-1 text-right font-medium">p90</th>
                  <th scope="col" className="py-1 text-right font-medium">Served</th>
                </tr>
              </thead>
              <tbody>
                {days.map((day) => (
                  <tr key={day.date} className="border-t border-border">
                    <th scope="row" className="py-1 text-left font-normal">{day.date}</th>
                    <td className="py-1 text-right">{day.p90 === null ? "—" : minutes(day.p90)}</td>
                    <td className="py-1 text-right">{day.served}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>

          <h2 className="mt-8 text-h2 font-semibold">Who did not arrive</h2>
          <table className="mt-2 w-full text-label">
            <caption className="text-left text-label text-muted">By service over the range</caption>
            <thead>
              <tr>
                <th scope="col" className="py-1 text-left font-medium">Service</th>
                <th scope="col" className="py-1 text-right font-medium">Joined</th>
                <th scope="col" className="py-1 text-right font-medium">No-shows</th>
                <th scope="col" className="py-1 text-right font-medium">Rate</th>
                <th scope="col" className="py-1 text-right font-medium">Cancelled</th>
              </tr>
            </thead>
            <tbody>
              {(misses.data as NoShow[]).map((row) => (
                <tr key={row.service_id ?? "walk-ins"} className="border-t border-border">
                  <th scope="row" className="py-1 text-left font-normal">
                    {row.service_id === null ? "Walk-ins with no service" : (names.get(row.service_id) ?? row.service_id.slice(0, 8))}
                  </th>
                  <td className="py-1 text-right">{row.joined}</td>
                  <td className="py-1 text-right">{row.no_shows}</td>
                  <td className="py-1 text-right">{percent(row.no_show_rate)}</td>
                  <td className="py-1 text-right">{row.cancelled}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

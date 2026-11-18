import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { addDays, localToday } from "@/lib/time";

export type PeakHour = components["schemas"]["PeakHourOut"];
export type WaitTime = components["schemas"]["WaitTimeOut"];
export type NoShow = components["schemas"]["NoShowOut"];

export const DAYS_SHOWN = 30;

/** The rollup runs on finished days, so the dashboard ends YESTERDAY in the business's own
 *  zone: today is still being lived through and would read as a half-empty day. */
export function range(zone: string, now: Date = new Date()): { from: string; to: string } {
  const to = addDays(localToday(zone, now), -1);
  return { from: addDays(to, -(DAYS_SHOWN - 1)), to };
}

const query = (businessId: string, name: string, span: { from: string; to: string }) =>
  `/v1/businesses/${businessId}/reports/${name}?from=${span.from}&to=${span.to}`;

export const peakHoursQuery = (businessId: string, span: { from: string; to: string }) => ({
  queryKey: ["report-peak-hours", businessId, span.from, span.to] as const,
  queryFn: () => api<PeakHour[]>(query(businessId, "peak-hours", span)),
});
export const waitTimesQuery = (businessId: string, span: { from: string; to: string }) => ({
  queryKey: ["report-wait-times", businessId, span.from, span.to] as const,
  queryFn: () => api<WaitTime[]>(query(businessId, "wait-times", span)),
});
export const noShowsQuery = (businessId: string, span: { from: string; to: string }) => ({
  queryKey: ["report-no-shows", businessId, span.from, span.to] as const,
  queryFn: () => api<NoShow[]>(query(businessId, "no-shows", span)),
});

/** Tokens per business-local hour, every hour of the day present so the bars line up. */
export function byHour(rows: PeakHour[]): { hour: number; tokens: number }[] {
  const hours = Array.from({ length: 24 }, (_, hour) => ({ hour, tokens: 0 }));
  for (const row of rows) hours[row.hour].tokens += row.tokens;
  return hours;
}

/** One point per day: the waits of every service that day, pooled by the tokens they served. */
export function byDay(rows: WaitTime[]): { date: string; served: number; avg: number; p90: number | null }[] {
  const days = new Map<string, { served: number; weighted: number; p90: number | null }>();
  for (const row of rows) {
    const day = days.get(row.date) ?? { served: 0, weighted: 0, p90: null };
    day.served += row.served;
    day.weighted += row.avg_wait_sec * row.served;
    if (row.p90_wait_sec !== null) day.p90 = Math.max(day.p90 ?? 0, row.p90_wait_sec);
    days.set(row.date, day);
  }
  return [...days.entries()]
    .map(([date, day]) => ({ date, served: day.served, avg: day.served ? Math.round(day.weighted / day.served) : 0, p90: day.p90 }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

export const minutes = (seconds: number) => Math.round(seconds / 60);

/** The business's own totals over the range, for the sentences above the charts. */
export function summary(peaks: PeakHour[], waits: WaitTime[], misses: NoShow[]) {
  const hours = byHour(peaks);
  const busiest = hours.reduce((best, hour) => (hour.tokens > best.tokens ? hour : best), hours[0]);
  const served = waits.reduce((total, row) => total + row.served, 0);
  const weighted = waits.reduce((total, row) => total + row.avg_wait_sec * row.served, 0);
  const joined = misses.reduce((total, row) => total + row.joined, 0);
  const noShows = misses.reduce((total, row) => total + row.no_shows, 0);
  return {
    busiestHour: busiest.tokens > 0 ? busiest.hour : null,
    busiestTokens: busiest.tokens,
    served,
    avgWaitSec: served ? Math.round(weighted / served) : 0,
    joined,
    noShows,
    noShowRate: joined ? noShows / joined : 0,
  };
}

export const clockHour = (hour: number) => `${String(hour).padStart(2, "0")}:00`;
export const percent = (rate: number) => `${Math.round(rate * 1000) / 10}%`;

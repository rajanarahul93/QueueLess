// The e2e queue businesses' zone: check-in tests need "today" in the business's zone with room on
// both sides (a slot an hour ahead, one that ended an hour ago), so "now" must be at least 2 hours
// from local midnight there. The browsers stay in Los Angeles, never the business zone.
const FALLBACKS = ["Asia/Tokyo", "Pacific/Auckland", "Asia/Dubai", "Europe/London", "America/Sao_Paulo", "America/New_York", "Pacific/Honolulu"];

const hourIn = (zone: string, now: Date) =>
  Number(new Intl.DateTimeFormat("en-US", { timeZone: zone, hour: "numeric", hourCycle: "h23" }).format(now));

/** Kolkata, unless it is within 2 hours of midnight there; then the listed zone nearest noon. */
export function queueZone(now: Date): { zone: string; why: string } {
  const kolkata = hourIn("Asia/Kolkata", now);
  if (kolkata >= 2 && kolkata < 22) return { zone: "Asia/Kolkata", why: `Kolkata is at ${kolkata}:xx, at least 2 hours from midnight` };
  const zone = FALLBACKS.reduce((best, candidate) =>
    Math.abs(hourIn(candidate, now) - 12) < Math.abs(hourIn(best, now) - 12) ? candidate : best,
  );
  return { zone, why: `Kolkata is at ${kolkata}:xx, within 2 hours of midnight; ${zone} is at ${hourIn(zone, now)}:xx, nearest noon` };
}

export { hourIn };

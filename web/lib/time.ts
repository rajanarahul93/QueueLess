// Times are shown in the business's own zone, never the browser's. API instants carry an
// offset; Intl formats them in the zone. Calendar dates stay "YYYY-MM-DD" strings.

const cache = new Map<string, Intl.DateTimeFormat>();

function formatter(zone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${zone}|${JSON.stringify(options)}`;
  let found = cache.get(key);
  if (!found) {
    found = new Intl.DateTimeFormat("en-US", { timeZone: zone, ...options });
    cache.set(key, found);
  }
  return found;
}

/** "9:30 AM" for an API instant, in the business zone. */
export function timeLabel(iso: string, zone: string): string {
  return formatter(zone, { hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}

/** Today's date in the business zone, as "YYYY-MM-DD". */
export function localToday(zone: string, now: Date = new Date()): string {
  const parts = formatter(zone, { year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** Calendar arithmetic on "YYYY-MM-DD" (no zone involved). */
export function addDays(day: string, days: number): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, date + days)).toISOString().slice(0, 10);
}

/** "Mon" / "2" / "Nov" / "Mon, Nov 2" for a calendar date. */
export function dayLabel(day: string, style: "weekday" | "date" | "month" | "long"): string {
  const [year, month, date] = day.split("-").map(Number);
  const noon = new Date(Date.UTC(year, month - 1, date, 12));
  const options: Intl.DateTimeFormatOptions =
    style === "weekday"
      ? { weekday: "short" }
      : style === "date"
        ? { day: "numeric" }
        : style === "month"
          ? { month: "short" }
          : { weekday: "short", month: "short", day: "numeric" };
  return formatter("UTC", options).format(noon);
}

/** A day button's accessible name, "Mon 2 Nov": it starts with the visible "Mon 2". */
export function dayButtonName(day: string): string {
  return `${dayLabel(day, "weekday")} ${dayLabel(day, "date")} ${dayLabel(day, "month")}`;
}

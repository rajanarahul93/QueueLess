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

/** The zone's UTC offset in milliseconds at that instant, from its own rules. */
function offsetMs(zone: string, at: Date): number {
  const parts = formatter(zone, { timeZoneName: "longOffset" }).formatToParts(at);
  const name = parts.find((item) => item.type === "timeZoneName")?.value ?? "GMT";
  const match = /GMT([+-])(\d{1,2})(?::(\d{2}))?/.exec(name);
  if (!match) return 0; // plain "GMT": the zone is on UTC at that moment
  const minutes = Number(match[2]) * 60 + Number(match[3] ?? 0);
  return (match[1] === "-" ? -1 : 1) * minutes * 60_000;
}

/** The instant local midnight starts on `day` in `zone`. Two passes, because the offset at UTC
 *  midnight can differ from the offset at local midnight on a day the clocks change. */
export function dayStart(day: string, zone: string): Date {
  const utcMidnight = Date.parse(`${day}T00:00:00Z`);
  const guess = new Date(utcMidnight - offsetMs(zone, new Date(utcMidnight)));
  return new Date(utcMidnight - offsetMs(zone, guess));
}

/** One local day in the business zone as absolute instants, for the API's `from`/`to`. A day
 *  the clocks change is 23 or 25 hours long, and this is what makes it so. */
export function dayRange(zone: string, now: Date = new Date()): { from: string; to: string } {
  const day = localToday(zone, now);
  return { from: dayStart(day, zone).toISOString(), to: dayStart(addDays(day, 1), zone).toISOString() };
}

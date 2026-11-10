/** The business's week, in its own wall-clock time. The API takes up to four intervals a day,
 *  half-open [open, close), and refuses open >= close (backend `schemas/business.py`). */

export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Weekday = (typeof WEEKDAYS)[number];
export type Interval = { open: string; close: string };
export type OpenHours = Partial<Record<Weekday, Interval[]>>;

export const MAX_INTERVALS = 4;
/** `open_hours` is HH:MM and cannot say 24:00, so the backend reads "23:59" as the end of the
 *  day (`_wall` in `app/services/booking.py`). A day that runs past midnight cannot be said at
 *  all; that is a Phase 7 question, not something this screen can fix. */
export const MIDNIGHT = "23:59";
export const WEEKDAY_LABEL: Record<Weekday, string> = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

/** What a new business starts with on the form: a plain working week the owner can change. */
export function defaultHours(): OpenHours {
  return Object.fromEntries(
    (["mon", "tue", "wed", "thu", "fri"] as Weekday[]).map((day) => [day, [{ open: "09:00", close: "17:00" }]]),
  );
}

/** The first thing wrong with the week, in the owner's words, or null. The server checks this
 *  too; saying it here means they do not have to press to find out. */
export function hoursProblem(hours: OpenHours): string | null {
  const days = WEEKDAYS.filter((day) => (hours[day] ?? []).length > 0);
  if (days.length === 0) return "Open on at least one day, so people can book.";
  for (const day of days) {
    const intervals = hours[day] ?? [];
    if (intervals.length > MAX_INTERVALS) return `${WEEKDAY_LABEL[day]} has more than ${MAX_INTERVALS} periods.`;
    for (const interval of intervals) {
      if (!interval.open || !interval.close) return `${WEEKDAY_LABEL[day]} needs an opening and a closing time.`;
      if (interval.open >= interval.close) return `${WEEKDAY_LABEL[day]} closes before it opens.`;
    }
  }
  return null;
}

/** Only the days that are open, as the API wants them. */
export function toApi(hours: OpenHours): Record<string, Interval[]> {
  return Object.fromEntries(WEEKDAYS.filter((day) => (hours[day] ?? []).length > 0).map((day) => [day, hours[day]!]));
}

"use client";

import { useRef } from "react";
import { Button } from "@/components/Button";
import { MAX_INTERVALS, MIDNIGHT, WEEKDAY_LABEL, WEEKDAYS, type Interval, type OpenHours, type Weekday } from "@/lib/owner/hours";

/** The week, one row per day, with native time inputs so a keyboard and a screen reader get
 *  the browser's own picker. The API requires open_hours when a business is created, so this
 *  ships with the create form rather than waiting for Settings.
 *
 *  Every day is on screen, including the ones that start closed: the hours a business keeps are
 *  never decided by a default the owner did not see. */
export function OpenHoursEditor({
  hours,
  onChange,
  error,
}: {
  hours: OpenHours;
  onChange: (next: OpenHours) => void;
  error?: string;
}) {
  const rows = useRef(new Map<Weekday, HTMLLIElement | null>());
  const set = (day: Weekday, intervals: Interval[]) => onChange({ ...hours, [day]: intervals });

  /** After removing a period, focus must not fall to the body: it goes to the first time input
   *  this day still has, or to the day's own open/closed button. */
  const keepFocus = (day: Weekday) => {
    const row = rows.current.get(day);
    requestAnimationFrame(() => {
      // A remaining time input first (that is where the work is), then the day's own button:
      // querySelector would otherwise hand back whichever comes first in the markup.
      const next = row?.querySelector<HTMLElement>("input[type=time]") ?? row?.querySelector<HTMLElement>("button");
      next?.focus();
    });
  };

  return (
    <fieldset aria-describedby={`hours-note${error ? " hours-error" : ""}`}>
      <legend className="text-label font-medium">Opening hours</legend>
      <p id="hours-note" className="mt-1 text-label text-muted">
        {`These are a suggestion to change, not a setting that happens by itself: every day is listed, and the
          times are the business's own clock. To stay open until midnight, close at 11:59 PM (${MIDNIGHT}).`}
      </p>
      {error && (
        <p id="hours-error" className="mt-1 text-label text-danger">
          {error}
        </p>
      )}
      <ul className="mt-2 space-y-3">
        {WEEKDAYS.map((day) => {
          const intervals = hours[day] ?? [];
          const name = WEEKDAY_LABEL[day];
          return (
            <li
              key={day}
              ref={(node) => {
                rows.current.set(day, node);
              }}
              className="rounded-md border border-border bg-surface p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-body font-medium">{name}</span>
                {intervals.length === 0 ? (
                  <Button type="button" variant="outline" onClick={() => set(day, [{ open: "09:00", close: "17:00" }])}>
                    {`Open ${name}`}
                  </Button>
                ) : (
                  <Button type="button" variant="outline" onClick={() => set(day, [])}>
                    {`Close ${name} all day`}
                  </Button>
                )}
              </div>
              {intervals.length === 0 ? (
                <p className="mt-1 text-label text-muted">Closed</p>
              ) : (
                <ul className="mt-2 space-y-2">
                  {intervals.map((interval, index) => (
                    <li key={index} className="flex flex-wrap items-end gap-2">
                      {(["open", "close"] as const).map((edge) => (
                        <div key={edge}>
                          <label htmlFor={`${day}-${edge}-${index}`} className="block text-label text-muted">
                            {edge === "open" ? "Opens" : "Closes"}
                          </label>
                          <input
                            id={`${day}-${edge}-${index}`}
                            type="time"
                            required
                            value={interval[edge]}
                            // The accessible name says which day and which period, so a screen
                            // reader never meets four identical "Opens" in a row.
                            aria-label={`${name}, period ${index + 1}, ${edge === "open" ? "opens" : "closes"}`}
                            onChange={(event) =>
                              set(
                                day,
                                intervals.map((item, at) => (at === index ? { ...item, [edge]: event.target.value } : item)),
                              )
                            }
                            className="mt-1 min-h-12 rounded-sm border border-border-strong bg-surface px-3 text-body"
                          />
                        </div>
                      ))}
                      {intervals.length > 1 && (
                        <Button
                          type="button"
                          variant="outline"
                          onClick={() => {
                            set(day, intervals.filter((_, at) => at !== index));
                            keepFocus(day);
                          }}
                        >
                          {`Remove ${name} period ${index + 1}`}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {intervals.length > 0 && intervals.length < MAX_INTERVALS && (
                <Button
                  type="button"
                  variant="outline"
                  className="mt-2"
                  onClick={() => set(day, [...intervals, { open: "18:00", close: "20:00" }])}
                >
                  {`Add a period on ${name}`}
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}

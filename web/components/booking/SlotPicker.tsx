"use client";

import { addDays, dayLabel, timeLabel } from "@/lib/time";
import type { Slot } from "@/lib/api/types";

export const DAYS_SHOWN = 7;
export const MAX_DAYS_AHEAD = 60; // the API's limit

/** Seven business-local days; earlier and later weeks within today..today+60. */
export function DateStrip({
  today,
  date,
  onPick,
}: {
  today: string;
  date: string;
  onPick: (date: string) => void;
}) {
  const offset = Math.floor(daysBetween(today, date) / DAYS_SHOWN) * DAYS_SHOWN;
  const first = addDays(today, offset);
  const last = addDays(today, MAX_DAYS_AHEAD);
  const days = Array.from({ length: DAYS_SHOWN }, (_, index) => addDays(first, index)).filter(
    (day) => day <= last,
  );
  return (
    <div className="mt-4">
      <div className="flex items-center justify-between">
        <button type="button" disabled={offset === 0} onClick={() => onPick(addDays(first, -DAYS_SHOWN))}
          className="min-h-11 px-2 text-label text-primary underline disabled:text-muted disabled:no-underline">
          Earlier
        </button>
        <span className="text-label text-muted">{dayLabel(date, "long")}</span>
        <button type="button" disabled={addDays(first, DAYS_SHOWN) > last} onClick={() => onPick(addDays(first, DAYS_SHOWN))}
          className="min-h-11 px-2 text-label text-primary underline disabled:text-muted disabled:no-underline">
          Later
        </button>
      </div>
      <div role="group" aria-label="Day" className="mt-2 grid grid-cols-7 gap-1">
        {days.map((day) => (
          <button key={day} type="button" aria-pressed={day === date} onClick={() => onPick(day)}
            className={`flex min-h-16 flex-col items-center justify-center rounded-md border text-label ${day === date ? "border-primary bg-primary text-primary-fg" : "border-border-strong bg-surface"}`}>
            {/* The name reads "Mon 2 Nov": the visible text first (WCAG 2.5.3), then the month. */}
            <span>{dayLabel(day, "weekday")}</span>{" "}
            <span className="text-body-lg font-semibold tabular-nums">{dayLabel(day, "date")}</span>{" "}
            <span className="sr-only">{dayLabel(day, "month")}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}

/** The free times as a radio group: arrow keys move between them, as with any radios. */
export function SlotGrid({
  slots,
  zone,
  selected,
  onSelect,
}: {
  slots: Slot[];
  zone: string;
  selected: string | null;
  onSelect: (slot: Slot) => void;
}) {
  return (
    <fieldset className="mt-6">
      <legend className="text-h2 font-semibold">Available times</legend>
      <div role="radiogroup" aria-label="Available times" className="mt-3 grid grid-cols-3 gap-2">
        {slots.map((slot) => {
          const label = timeLabel(slot.start, zone);
          const checked = slot.start === selected;
          return (
            <label key={slot.start}
              className={`flex min-h-12 cursor-pointer items-center justify-center rounded-md border text-label font-medium tabular-nums has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-focus ${checked ? "border-primary bg-primary text-primary-fg" : "border-border-strong bg-surface"}`}>
              <input type="radio" name="slot" value={slot.start} checked={checked}
                onChange={() => onSelect(slot)} className="sr-only" />
              {label}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

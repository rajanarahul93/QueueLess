"use client";

import { useRef, type ClipboardEvent, type KeyboardEvent } from "react";

const LENGTH = 6;

/** Six one-digit boxes. Typing moves forward, Backspace moves back, a paste fills them all,
 *  and the sixth digit calls onComplete. `value` is the digits entered so far. */
export function OtpInput({
  value,
  onChange,
  onComplete,
  invalid,
  describedBy,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  onComplete: (code: string) => void;
  invalid?: boolean;
  describedBy?: string;
  disabled?: boolean;
}) {
  const boxes = useRef<(HTMLInputElement | null)[]>([]);

  function set(next: string) {
    const digits = next.replace(/\D/g, "").slice(0, LENGTH);
    onChange(digits);
    boxes.current[Math.min(digits.length, LENGTH - 1)]?.focus();
    if (digits.length === LENGTH) onComplete(digits);
  }

  function onInput(index: number, typed: string) {
    const digit = typed.replace(/\D/g, "").slice(-1);
    if (!digit) return;
    set((value.slice(0, index) + digit).slice(0, LENGTH));
  }

  function onKeyDown(index: number, event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Backspace") {
      event.preventDefault();
      const end = value.length > index ? index : index - 1;
      set(value.slice(0, Math.max(0, end)));
    }
  }

  function onPaste(event: ClipboardEvent<HTMLInputElement>) {
    event.preventDefault();
    set(event.clipboardData.getData("text"));
  }

  return (
    <div role="group" aria-label="Verification code" aria-describedby={describedBy} className="flex gap-2">
      {Array.from({ length: LENGTH }, (_, index) => (
        <input
          key={index}
          ref={(element) => {
            boxes.current[index] = element;
          }}
          aria-label={`Digit ${index + 1}`}
          inputMode="numeric"
          autoComplete={index === 0 ? "one-time-code" : "off"}
          maxLength={1}
          disabled={disabled}
          value={value[index] ?? ""}
          onChange={(event) => onInput(index, event.target.value)}
          onKeyDown={(event) => onKeyDown(index, event)}
          onPaste={onPaste}
          aria-invalid={invalid || undefined}
          className={`h-14 w-12 rounded-sm bg-surface text-center text-h2 tabular-nums ${invalid ? "border-2 border-danger" : "border border-border-strong"}`}
        />
      ))}
    </div>
  );
}

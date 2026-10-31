import type { InputHTMLAttributes } from "react";

/** Label, input (16 px, so iOS does not zoom) and an error linked by aria-describedby. */
export function TextField({
  id,
  label,
  error,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { id: string; label: string; error?: string }) {
  return (
    <div>
      <label htmlFor={id} className="block text-label font-medium">
        {label}
      </label>
      <input
        id={id}
        {...props}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        className={`mt-1 block min-h-12 w-full rounded-sm border bg-surface px-3 text-body ${error ? "border-2 border-danger" : "border-border-strong"}`}
      />
      {error && (
        <p id={`${id}-error`} className="mt-1 text-label text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

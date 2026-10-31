import type { ButtonHTMLAttributes } from "react";

/** Primary (filled) or outline; 48 px high, label type; disabled while loading. */
export function Button({
  variant = "primary",
  loading = false,
  className = "",
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "outline"; loading?: boolean }) {
  const look =
    variant === "primary"
      ? "bg-primary text-primary-fg"
      : "border border-border-strong bg-surface text-text";
  return (
    <button
      {...props}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`inline-flex min-h-12 items-center justify-center rounded-md px-4 text-label font-medium disabled:opacity-50 ${look} ${className}`}
    >
      {loading ? "Please wait…" : children}
    </button>
  );
}

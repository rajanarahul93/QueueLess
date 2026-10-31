import { ApiError } from "@/lib/api/client";

// The one place problem codes become words. The server's `detail` is never shown to customers.
const COPY: Record<string, string> = {
  NETWORK: "You're offline or the server can't be reached. Check your connection and try again.",
  OTP_INVALID: "That code isn't right. Check it and try again.",
  OTP_EXPIRED: "That code has expired. Request a new one.",
  RATE_LIMITED: "Too many attempts. Please wait and try again.",
  SERVICE_UNAVAILABLE: "We can't do that right now. Please try again in a moment.",
  UNAUTHENTICATED: "Your email or password isn't right.",
  FORBIDDEN: "You don't have access to this.",
  NOT_FOUND: "We couldn't find that.",
  VALIDATION_ERROR: "Some details aren't valid. Check the highlighted fields.",
  INTERNAL_ERROR: "Something went wrong on our side. Please try again.",
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfterSec) {
      return `Too many attempts. Try again in ${formatWait(error.retryAfterSec)}.`;
    }
    return COPY[error.code] ?? COPY.INTERNAL_ERROR;
  }
  return COPY.INTERNAL_ERROR;
}

function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.ceil(seconds / 60);
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

/** Field paths the server rejected, e.g. ["email"], from a VALIDATION_ERROR body. */
export function invalidFields(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.code !== "VALIDATION_ERROR") return [];
  const errors = Array.isArray(error.body.errors) ? error.body.errors : [];
  return errors
    .map((item) => (Array.isArray(item?.loc) ? String(item.loc[item.loc.length - 1]) : ""))
    .filter(Boolean);
}

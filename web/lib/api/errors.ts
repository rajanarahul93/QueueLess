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
  SLOT_TAKEN: "That time was just taken. Pick another.",
  BOOKING_LIMIT_REACHED: "You already have 5 upcoming appointments. Cancel one to book another.",
  BOOKING_RESTRICTED:
    "Booking is paused on your account after missed appointments. You can still join the queue in person.",
  IDEMPOTENCY_CONFLICT: "Your booking is still being processed. Check your appointments before trying again.",
  RESCHEDULE_CUTOFF: "This appointment starts too soon to move. You can still cancel it.",
  INVALID_TRANSITION: "This appointment can't be changed any more.",
  QUEUE_CLOSED: "This queue isn't taking new people right now.",
  WALK_INS_FULL: "Walk-ins are full for today.",
  ALREADY_IN_QUEUE: "You already have a ticket in this queue.",
};

/** Check-in refusals, by the server's stable `reason` (never its free text). */
export const CHECK_IN_REASON: Record<string, string> = {
  too_early: "Check-in opens 30 minutes before your appointment time.",
  too_late: "Your appointment time has passed. You can still join the queue as a walk-in.",
  not_booked: "This appointment can't be checked in: it was cancelled, moved or already checked in.",
};

/** Every code the booking screens can receive; each has its own message above. */
export const BOOKING_CODES = [
  "SLOT_TAKEN",
  "BOOKING_LIMIT_REACHED",
  "BOOKING_RESTRICTED",
  "RATE_LIMITED",
  "IDEMPOTENCY_CONFLICT",
  "SERVICE_UNAVAILABLE",
  "NOT_FOUND",
  "FORBIDDEN",
  "VALIDATION_ERROR",
  "NETWORK",
] as const;

export function hasCopy(code: string): boolean {
  return code in COPY;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfterSec) {
      return `Too many attempts. Try again in ${formatWait(error.retryAfterSec)}.`;
    }
    if (error.code === "SERVICE_UNAVAILABLE" && error.retryAfterSec) {
      return `We can't do that right now. Try again in ${formatWait(error.retryAfterSec)}.`;
    }
    return COPY[error.code] ?? COPY.INTERNAL_ERROR;
  }
  return COPY.INTERNAL_ERROR;
}

/** Checking in or joining: the reason-specific refusals, and the hourly limit the two share. */
export function ticketMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "RATE_LIMITED") {
    return `You've joined or checked in many times this hour. Try again in ${formatWait(error.retryAfterSec ?? 60)}.`;
  }
  const reason = error instanceof ApiError ? error.body.reason : undefined;
  if (error instanceof ApiError && error.code === "INVALID_TRANSITION" && typeof reason === "string" && reason in CHECK_IN_REASON) {
    return CHECK_IN_REASON[reason];
  }
  return errorMessage(error);
}

/** Leaving the queue: the one refusal is a ticket already being served. */
export function leaveMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "INVALID_TRANSITION") {
    return "You're already being served, so you can't leave the queue now.";
  }
  return errorMessage(error);
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

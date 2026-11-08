import { ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { SnapshotToken, StaffAppointment } from "@/lib/api/types";

/** The words for an appointment's status (Design.md). The server's vocabulary is the DB's. */
export const APPOINTMENT_LABEL: Record<string, string> = {
  booked: "Booked",
  checked_in: "Checked in",
  in_service: "In service",
  completed: "Completed",
  cancelled: "Cancelled",
  no_show: "No-show",
  rescheduled: "Rescheduled",
};

/** The list asks for whole pages and follows the cursor; the cap stops a server that always
 *  answers with one from looping for ever. 10 x 50 is far more than a day of appointments. */
export const PAGE_LIMIT = 50;
export const MAX_PAGES = 10;
export const CAPPED_NOTICE = `Showing the first ${PAGE_LIMIT * MAX_PAGES} appointments`;

/** Bookings made elsewhere reach this screen by the poll, not by a stream: no appointment
 *  event exists, so the only live signal is a token delta from somebody checking in. */
export const POLL_MS = 30_000;
/** A delta burst settles before the list is read again, as the queue's own refetch does. */
export const REFETCH_AFTER_DELTA_MS = 2000;

export type RowAction = "check-in" | "open" | "none";

/** What a row offers. The client knows the status and whether a token exists; the SERVER owns
 *  the check-in window, so a booked row keeps its button even when its slot looks past. */
export function rowAction(status: string, hasToken: boolean): RowAction {
  if (status === "booked") return "check-in";
  if (hasToken && (status === "checked_in" || status === "in_service")) return "open";
  return "none";
}

/** The token this appointment got when it was checked in, as today's queue now has it. */
export function tokenFor(
  appointment: StaffAppointment,
  tokens: readonly SnapshotToken[],
): SnapshotToken | undefined {
  return tokens.find((token) => token.appointment_id === appointment.id);
}

/** Staff words for a refused check-in. The customer screens say "your appointment"; here it is
 *  somebody else's, and the hourly limit is the one walk-in joins share. */
export function checkInMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return errorMessage(error);
  const reason = typeof error.body.reason === "string" ? error.body.reason : "";
  if (error.code === "INVALID_TRANSITION") {
    if (reason === "too_early") return "Check-in opens 30 minutes before the appointment time.";
    if (reason === "too_late") return "That appointment time has passed. They can still join the queue as a walk-in.";
    return "This appointment can't be checked in: it was cancelled, moved or already finished.";
  }
  switch (error.code) {
    case "ALREADY_IN_QUEUE":
      return "This customer already has a ticket in today's queue.";
    case "QUEUE_CLOSED":
      return "This queue isn't taking check-ins right now.";
    case "NOT_FOUND":
      return "This appointment isn't there any more. The list is up to date now.";
    case "FORBIDDEN":
      return "Only this business's staff and owner can check people in.";
    case "RATE_LIMITED":
      return `Too many check-ins and joins this hour on this account. Try again in ${waitLabel(error.retryAfterSec ?? 60)}.`;
    default:
      return errorMessage(error); // 503 with its countdown, network, the rest
  }
}

function waitLabel(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.ceil(seconds / 60);
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

/** What the polite line says after a check-in lands. A repeat answers 200 with the same token,
 *  which is a success: somebody got there first, and the screen now agrees with the server. */
export function checkedInNotice(name: string, tokenNo: number, created: boolean): string {
  return created ? `Checked in ${name}, token #${tokenNo}` : `Already checked in, token #${tokenNo}`;
}

/** What the polite line says when somebody else's change relabelled a row. */
export function changedRowNotice(name: string, status: string): string {
  return `${name} is now ${(APPOINTMENT_LABEL[status] ?? status).toLowerCase()}`;
}

import { ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { SnapshotToken } from "@/lib/api/types";

/** The server's rule (backend `transitions.py`): a `skip` is allowed while fewer than this many
 *  have happened; the next one makes the token a no-show instead. */
export const MAX_SKIPS = 2;

export type Primary = "call-next" | "start" | "complete";

/** How long the primary button is held after someone ELSE's change renamed it. A click lands on
 *  whatever the label says at that moment, so a finger already moving towards "Start" must not
 *  press a button that has just become "Complete". Long enough to notice, short enough not to
 *  feel broken. */
export const PRIMARY_HOLD_MS = 700;

/** Does this change of the primary button deserve a hold? Only a change of KIND, caused by data
 *  that arrived rather than by this user's own press, and never the first render. */
export function holdOnChange<Kind extends string>(previous: Kind | null, next: Kind, ownPress: boolean): boolean {
  if (previous === null) return false; // first render, first load
  if (previous === next) return false; // a version bump, a priority or count change
  return !ownPress;
}

const STATUS_WORDS: Record<string, string> = { waiting: "waiting", called: "called", in_service: "in service" };

/** What the polite line says when someone else's change renamed the button. */
export function changedNotice(tokenNo: number, status: string): string {
  return `#${tokenNo} is now ${STATUS_WORDS[status] ?? status}`;
}

/** The action bar's primary button, from the target token (the selected one, else the last one
 *  this tab called). Call next always takes the next token in the queue's own order, never the
 *  selected one, which is why no target, a waiting target and an ended target all mean the same. */
export function primaryAction(target: SnapshotToken | undefined): Primary {
  if (!target) return "call-next";
  if (target.status === "called") return "start";
  if (target.status === "in_service") return "complete";
  return "call-next"; // waiting, or a status that has left the queue
}

export const PRIMARY_LABEL: Record<Primary, string> = {
  "call-next": "Call next",
  start: "Start",
  complete: "Complete",
};

/** Skips left before a skip becomes a no-show; 0 means the next press is the no-show. */
export function skipsLeft(target: SnapshotToken): number {
  return Math.max(0, MAX_SKIPS - target.skip_count);
}

/** "Skip (2/2)" counts the skips still available (Design.md); at zero the press is a no-show,
 *  so it is labelled and confirmed as one. */
export function skipLabel(target: SnapshotToken): string {
  const left = skipsLeft(target);
  return left === 0 ? "No-show" : `Skip (${left}/${MAX_SKIPS})`;
}

export const canSkip = (target: SnapshotToken | undefined) => target?.status === "called";
export const canSetPriority = (target: SnapshotToken | undefined) => target?.status === "waiting";

export const PRIORITY_LABEL: Record<number, string> = { 0: "Normal", 1: "Priority", 2: "Urgent" };

/** What the staff member reads after their own action, and after someone else's change. */
export const CONFLICT_NOTICE = "Someone else changed this";

/** The console's own words for what these endpoints can refuse. Nothing is retried here: a
 *  conflict means the screen was out of date, so the staff member decides what to do next. */
export function actionMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return errorMessage(error);
  switch (error.code) {
    case "VERSION_CONFLICT":
      return `${CONFLICT_NOTICE}. The list is up to date now; check it and try again.`;
    case "INVALID_TRANSITION":
      return "That is no longer possible for this token. The list is up to date now.";
    case "NOT_FOUND":
      return "This token isn't there any more. The list is up to date now.";
    case "FORBIDDEN":
      return "Only this business's staff and owner can do that.";
    case "QUEUE_CLOSED":
      return "This isn't today's queue, so it can't be served.";
    case "VALIDATION_ERROR":
      return "That isn't a valid choice. Pick a service and try again.";
    default:
      return errorMessage(error); // RATE_LIMITED (with its countdown), 503, network, the rest
  }
}

/** What the polite line says after an action lands. */
export function actionAnnouncement(action: Primary | "skip" | "no-show" | "priority", tokenNo: number | null): string {
  switch (action) {
    case "call-next":
      return tokenNo === null ? "Nobody is waiting" : `Called #${tokenNo}`;
    case "start":
      return `Started #${tokenNo}`;
    case "complete":
      return `Completed #${tokenNo}`;
    case "skip":
      return `Skipped #${tokenNo}`;
    case "no-show":
      return `Marked #${tokenNo} no-show`;
    case "priority":
      return "Priority set";
  }
}

/** What became of a token that has left the active list, as far as the last delta says. */
export function endedNotice(tokenNo: number, status: string | undefined): string {
  const words: Record<string, string> = { completed: "completed", no_show: "no-show", cancelled: "cancelled" };
  const word = status === undefined ? undefined : words[status];
  return word ? `#${tokenNo} ${word}` : `#${tokenNo} is no longer in the queue`;
}

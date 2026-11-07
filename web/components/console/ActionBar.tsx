"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { api } from "@/lib/api/client";
import type { Service, SnapshotToken } from "@/lib/api/types";
import {
  actionMessage,
  canSetPriority,
  canSkip,
  PRIMARY_LABEL,
  PRIORITY_LABEL,
  primaryAction,
  skipLabel,
  skipsLeft,
} from "@/lib/console/actions";
import type { Press } from "@/lib/console/useAction";

/** Only the services a walk-in can be started with: the endpoint leaves inactive ones out. */
export const servicesQuery = (businessId: string) => ({
  queryKey: ["services", businessId] as const,
  queryFn: () => api<Service[]>(`/v1/businesses/${businessId}/services`),
  staleTime: 60_000,
});

/** The staff action bar: one state-driven primary button, Skip, No-show and the priority
 *  control, all acting on the target token. Nothing here guesses the outcome: the screen
 *  changes when the server answers. */
export function ActionBar({
  businessId,
  target,
  busy,
  slow,
  held = false,
  error,
  onPress,
}: {
  businessId: string;
  target: SnapshotToken | undefined;
  busy: boolean;
  slow: boolean;
  /** Someone else's change just renamed the primary button; it waits a moment (PRIMARY_HOLD_MS)
   *  so a click already on its way cannot press the new action. Only the primary is held. */
  held?: boolean;
  error: unknown;
  onPress: (press: Press) => void;
}) {
  const primary = primaryAction(target);
  // The choice belongs to one token: a different target starts from nothing, with no effect.
  const [choice, setChoice] = useState<{ forToken: string | undefined; serviceId: string }>({ forToken: undefined, serviceId: "" });
  const serviceId = choice.forToken === target?.id ? choice.serviceId : "";
  const [confirming, setConfirming] = useState(false);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  // A walk-in must be given a service at Start; an appointment token already has one, and
  // sending one for it is refused by the server, so the picker is not shown at all.
  const needsService = primary === "start" && target?.appointment_id === null;
  const services = useQuery({ ...servicesQuery(businessId), enabled: needsService });

  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (confirming && !node.open) node.showModal();
    if (!confirming && node.open) node.close();
  }, [confirming]);

  const noShowPress = () => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setConfirming(true);
  };
  const dismiss = () => {
    setConfirming(false);
    opener.current?.focus();
  };

  const disabled = busy && !slow;
  const primaryBlocked = disabled || held || (needsService && !serviceId);
  const skipping = canSkip(target);
  const noShowNext = Boolean(target && skipping && skipsLeft(target) === 0); // the skip that becomes a no-show

  return (
    <div className="mt-6 rounded-md border border-border bg-surface p-4">
      {needsService && (
        <div className="mb-3">
          <label htmlFor="start-service" className="block text-label text-muted">
            Service for this walk-in
          </label>
          <select
            id="start-service"
            value={serviceId}
            disabled={disabled}
            onChange={(event) => setChoice({ forToken: target?.id, serviceId: event.target.value })}
            className="mt-1 min-h-12 w-full rounded-md border border-border-strong bg-surface px-3 text-body"
          >
            <option value="">Choose a service…</option>
            {(services.data ?? []).map((service) => (
              <option key={service.id} value={service.id}>
                {service.name}
              </option>
            ))}
          </select>
        </div>
      )}
      <div role="group" aria-label="Queue actions" className="flex flex-wrap items-center gap-2">
        <Button
          disabled={primaryBlocked}
          onClick={() =>
            onPress(
              primary === "call-next"
                ? { kind: "call-next" }
                : primary === "start"
                  ? { kind: "start", tokenId: target!.id, serviceId: needsService ? serviceId : null }
                  : { kind: "complete", tokenId: target!.id },
            )
          }
        >
          {PRIMARY_LABEL[primary]}
        </Button>
        <Button variant="outline" disabled={disabled || !skipping} onClick={() => (noShowNext ? noShowPress() : onPress({ kind: "skip", tokenId: target!.id }))}>
          {target && skipping ? skipLabel(target) : "Skip"}
        </Button>
        <Button variant="outline" className="text-danger" disabled={disabled || !skipping} onClick={noShowPress}>
          No-show
        </Button>
      </div>
      {canSetPriority(target) && (
        <div role="group" aria-label="Priority" className="mt-3 flex flex-wrap items-center gap-2">
          {[0, 1, 2].map((value) => (
            <Button
              key={value}
              variant={target!.priority === value ? "primary" : "outline"}
              aria-pressed={target!.priority === value}
              disabled={disabled}
              onClick={() => onPress({ kind: "priority", tokenId: target!.id, priority: value })}
            >
              {PRIORITY_LABEL[value]}
            </Button>
          ))}
        </div>
      )}
      {slow && <p className="mt-2 text-label text-muted">Still working… check the list.</p>}
      {error !== null && <Alert>{actionMessage(error)}</Alert>}
      <dialog
        ref={dialog}
        onCancel={(event) => {
          event.preventDefault(); // Esc keeps the token as it is
          dismiss();
        }}
        onClose={() => setConfirming(false)}
        className="max-w-[360px] rounded-md border border-border bg-surface p-4 text-text backdrop:bg-black/40"
        aria-labelledby="no-show-title"
      >
        <h2 id="no-show-title" className="text-body-lg font-semibold">
          Mark #{target?.token_no} as not here?
        </h2>
        <p className="mt-2 text-body text-muted">
          They lose their place in the queue{noShowNext ? " (this is their last skip)" : ""}. This cannot be undone.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={dismiss}>
            Keep waiting
          </Button>
          <Button
            className="text-danger"
            onClick={() => {
              setConfirming(false);
              if (target) onPress({ kind: "no-show", tokenId: target.id });
            }}
          >
            Yes, not here
          </Button>
        </div>
      </dialog>
    </div>
  );
}

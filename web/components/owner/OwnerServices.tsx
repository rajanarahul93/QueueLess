"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Form } from "@/components/Form";
import { TextField } from "@/components/TextField";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { errorMessage, invalidFields } from "@/lib/api/errors";
import { returnFocus } from "@/lib/focus";
import {
  asForm,
  blankForm,
  changedFields,
  formProblems,
  servicesQuery,
  toCreate,
  type ServiceForm,
} from "@/lib/owner/services";
import { useKeyedWrite } from "@/lib/owner/useKeyedWrite";
import type { Service } from "@/lib/api/types";

const FIELD_COPY: Record<string, string> = {
  name: "Give the service a name.",
  duration_min: "Check the duration.",
  buffer_min: "Check the buffer.",
  price: "Check the price.",
};

/** The owner's services for one business: what they are, what they cost, and which ones are
 *  being offered. There is no delete in the API and none is pretended here - a service is
 *  switched off instead, which stops new bookings while every appointment already made keeps
 *  the service it was booked with. */
export function OwnerServices({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient();
  const list = useQuery({ ...servicesQuery(businessId), retry: false });
  const business = useQuery({ ...businessQuery(businessId), retry: false });
  const { run, busy, slow, error, forget } = useKeyedWrite<Service>();

  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<ServiceForm>(blankForm);
  const [problems, setProblems] = useState<Partial<Record<keyof ServiceForm, string>>>({});
  const [confirming, setConfirming] = useState<Service | null>(null);
  const [said, setSaid] = useState<string>("");
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const rowBusy = useRef<string | null>(null);

  const serverFields = invalidFields(error);
  const invalid = (field: keyof ServiceForm) =>
    problems[field] ?? (serverFields.includes(field) ? FIELD_COPY[field] : undefined);

  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (confirming && !node.open) node.showModal();
    if (!confirming && node.open) node.close();
  }, [confirming]);

  /** Nothing optimistic: the list is read again and the screen shows what the server has. */
  async function reload() {
    await queryClient.invalidateQueries({ queryKey: servicesQuery(businessId).queryKey });
  }

  function start(service: Service | null, button: HTMLButtonElement | null) {
    opener.current = button;
    setEditing(service ? service.id : "new");
    setForm(service ? asForm(service) : blankForm());
    setProblems({});
    forget();
    setSaid("");
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const wrong = formProblems(form);
    setProblems(wrong);
    if (Object.keys(wrong).length > 0) {
      // The first field in error takes focus, so a keyboard user lands on what to fix.
      const order: [keyof ServiceForm, string][] = [
        ["name", "service-name"],
        ["duration_min", "service-duration"],
        ["buffer_min", "service-buffer"],
        ["price", "service-price"],
      ];
      const first = order.find(([field]) => wrong[field]);
      if (first) document.getElementById(first[1])?.focus();
      return;
    }
    const target = list.data?.find((service) => service.id === editing);
    if (target) {
      const patch = changedFields(target, form);
      if (Object.keys(patch).length === 0) {
        setEditing(null); // nothing changed: nothing to send
        return;
      }
      const saved = await run(`/v1/businesses/${businessId}/services/${target.id}`, "PATCH", patch);
      if (saved === "failed") return;
      setSaid(`Saved ${saved.name}`);
    } else {
      const made = await run(`/v1/businesses/${businessId}/services`, "POST", toCreate(form));
      if (made === "failed") return;
      setSaid(`Added ${made.name}`);
    }
    setEditing(null);
    await reload();
  }

  async function setActive(service: Service, active: boolean) {
    if (rowBusy.current === service.id) return; // one action per row at a time
    rowBusy.current = service.id;
    try {
      const saved = await run(`/v1/businesses/${businessId}/services/${service.id}`, "PATCH", { is_active: active });
      if (saved === "failed") return;
      setSaid(`${active ? "Offering" : "Stopped offering"} ${saved.name}`);
      await reload();
    } finally {
      rowBusy.current = null;
    }
  }

  if (list.isError) {
    return (
      <section className="mx-auto max-w-[640px] px-4 py-8">
        <h1 className="text-h1 font-semibold">Services</h1>
        <Alert>{errorMessage(list.error)}</Alert>
        <Button type="button" className="mt-4" onClick={() => void list.refetch()}>
          Retry
        </Button>
      </section>
    );
  }

  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Services</h1>
      <p className="mt-1 text-label text-muted">{business.data?.name ?? ""}</p>
      <p role="status" className="mt-2 text-label text-muted">
        {said}
      </p>

      {!list.data ? (
        <DelayedSkeleton rows={3} />
      ) : (
        <>
          {list.data.length === 0 ? (
            <p className="mt-4 text-body text-muted">No services yet. Add the first one people can book.</p>
          ) : (
            <ul className="mt-4 space-y-3">
              {list.data.map((service) => (
                <li key={service.id} className="rounded-md border border-border bg-surface p-4">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <h2 className="text-body-lg font-semibold">{service.name}</h2>
                      <p className="mt-1 text-label text-muted">
                        {service.duration_min} min
                        {service.buffer_min > 0 ? ` + ${service.buffer_min} min buffer` : ""} · {service.price}
                      </p>
                    </div>
                    <p className="text-label text-muted">{service.is_active ? "Offered" : "Not offered"}</p>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" variant="outline" onClick={(event) => start(service, event.currentTarget)}>
                      {`Edit ${service.name}`}
                    </Button>
                    {service.is_active ? (
                      <Button
                        type="button"
                        variant="outline"
                        onClick={(event) => {
                          opener.current = event.currentTarget;
                          setConfirming(service);
                        }}
                      >
                        {`Stop offering ${service.name}`}
                      </Button>
                    ) : (
                      <Button type="button" variant="outline" onClick={() => void setActive(service, true)}>
                        {`Offer ${service.name} again`}
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {editing === null ? (
            <Button type="button" className="mt-4" onClick={(event) => start(null, event.currentTarget)}>
              Add a service
            </Button>
          ) : (
            <Form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
              <h2 className="text-h2 font-semibold">{editing === "new" ? "Add a service" : "Edit the service"}</h2>
              <TextField
                id="service-name"
                label="Name"
                value={form.name}
                maxLength={120}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                error={invalid("name")}
              />
              <TextField
                id="service-duration"
                label="Duration (minutes)"
                inputMode="numeric"
                value={form.duration_min}
                onChange={(event) => setForm({ ...form, duration_min: event.target.value })}
                error={invalid("duration_min")}
              />
              <TextField
                id="service-buffer"
                label="Buffer after it (minutes)"
                inputMode="numeric"
                value={form.buffer_min}
                onChange={(event) => setForm({ ...form, buffer_min: event.target.value })}
                error={invalid("buffer_min")}
              />
              <TextField
                id="service-price"
                label="Price"
                inputMode="decimal"
                value={form.price}
                onChange={(event) => setForm({ ...form, price: event.target.value })}
                error={invalid("price")}
              />
              {error !== null && serverFields.length === 0 && <Alert>{errorMessage(error)}</Alert>}
              {slow && busy && <p className="text-label text-muted">Still working… check the list.</p>}
              <div className="flex flex-wrap gap-2">
                <Button type="submit" disabled={busy && !slow} loading={busy && !slow}>
                  Save
                </Button>
                <Button type="button" variant="outline" onClick={() => setEditing(null)}>
                  Cancel
                </Button>
              </div>
            </Form>
          )}
        </>
      )}

      <dialog
        ref={dialog}
        aria-labelledby="stop-offering-title"
        onCancel={(event) => {
          event.preventDefault(); // Esc keeps the service as it is
          setConfirming(null);
        }}
        onClose={() => {
          setConfirming(null);
          // The shared helper, not a bare focus(): nothing on this screen takes focus after an
          // action, so today the two behave the same (a mutation to an unconditional focus()
          // fails no test). It is the house rule for dialogs, and it is what keeps this screen
          // right the day something here focuses a notice, which is where it bit the cancel flow.
          returnFocus(opener.current, dialog.current);
        }}
        className="m-auto w-[calc(100%-2rem)] max-w-[420px] rounded-lg border border-border bg-surface p-6 text-text shadow-2 backdrop:bg-black/40"
      >
        <h2 id="stop-offering-title" className="text-h2 font-semibold">
          Stop offering this service?
        </h2>
        <p className="mt-2 text-body text-muted">
          {confirming
            ? `Nobody can book ${confirming.name} from now on. Appointments already booked keep it, and you can offer it again whenever you like.`
            : ""}
        </p>
        <div className="mt-6 flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => setConfirming(null)}>
            Keep it
          </Button>
          <Button
            type="button"
            disabled={busy && !slow}
            loading={busy && !slow}
            onClick={() => {
              const target = confirming;
              setConfirming(null);
              if (target) void setActive(target, false);
            }}
          >
            Stop offering it
          </Button>
        </div>
      </dialog>
    </section>
  );
}

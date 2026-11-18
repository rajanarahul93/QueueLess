"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Form } from "@/components/Form";
import { TextField } from "@/components/TextField";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { api } from "@/lib/api/client";
import { errorMessage, invalidFields } from "@/lib/api/errors";
import { returnFocus } from "@/lib/focus";
import { blankStaff, staffProblems, staffQuery, type StaffForm } from "@/lib/owner/staff";
import { useKeyedWrite } from "@/lib/owner/useKeyedWrite";
import type { Staff } from "@/lib/api/types";

const FIELD_COPY: Record<string, string> = {
  name: "Give the person a name.",
  email: "Check the email address.",
  initial_password: "That first password is not long enough.",
};

/** The people who can run this business's queue. The API offers add and remove only: there is no
 *  rename, no role change, no invite and no password reset, so the owner types the first password
 *  here and tells the person themselves. It is held just long enough to send and then dropped -
 *  never read back from the server, never stored, never logged. */
export function OwnerStaff({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient();
  const list = useQuery({ ...staffQuery(businessId), retry: false });
  const business = useQuery({ ...businessQuery(businessId), retry: false });
  const { run, busy, slow, error, forget } = useKeyedWrite<Staff>();

  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<StaffForm>(blankStaff);
  const [reveal, setReveal] = useState(false);
  const [problems, setProblems] = useState<Partial<Record<keyof StaffForm, string>>>({});
  const [removing, setRemoving] = useState<Staff | null>(null);
  const [said, setSaid] = useState("");
  const [removeError, setRemoveError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const inFlight = useRef<string | null>(null);

  const serverFields = invalidFields(error);
  const invalid = (field: keyof StaffForm) =>
    problems[field] ?? (serverFields.includes(field === "entry" ? "initial_password" : field) ? FIELD_COPY[field === "entry" ? "initial_password" : field] : undefined);

  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (removing && !node.open) node.showModal();
    if (!removing && node.open) node.close();
  }, [removing]);

  async function reload() {
    await queryClient.invalidateQueries({ queryKey: staffQuery(businessId).queryKey });
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const wrong = staffProblems(form);
    setProblems(wrong);
    if (Object.keys(wrong).length > 0) {
      const order: [keyof StaffForm, string][] = [["name", "staff-name"], ["email", "staff-email"], ["entry", "staff-entry"]];
      const first = order.find(([field]) => wrong[field]);
      if (first) document.getElementById(first[1])?.focus();
      return;
    }
    const made = await run(`/v1/businesses/${businessId}/staff`, "POST", {
      name: form.name.trim(),
      email: form.email.trim(),
      initial_password: form.entry,
    });
    // Whatever happened, the first password does not stay in this page's memory.
    setForm({ ...blankStaff(), name: form.name, email: form.email });
    setReveal(false);
    if (made === "failed") return;
    setForm(blankStaff());
    setAdding(false);
    setSaid(`Added ${made.name}`);
    await reload();
  }

  async function remove(person: Staff) {
    if (inFlight.current === person.id) return; // one removal at a time, per row
    inFlight.current = person.id;
    setRemoveError(null);
    try {
      // DELETE takes no Idempotency-Key (the API does not offer one here) and is safe to repeat.
      await api(`/v1/businesses/${businessId}/staff/${person.id}`, { method: "DELETE" });
      setSaid(`Removed ${person.name}`);
      await reload();
    } catch (failure) {
      setRemoveError(errorMessage(failure));
    } finally {
      inFlight.current = null;
    }
  }

  if (list.isError) {
    return (
      <section className="mx-auto max-w-[640px] px-4 py-8">
        <h1 className="text-h1 font-semibold">Staff</h1>
        <Alert>{errorMessage(list.error)}</Alert>
        <Button type="button" className="mt-4" onClick={() => void list.refetch()}>
          Retry
        </Button>
      </section>
    );
  }

  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Staff</h1>
      <p className="mt-1 text-label text-muted">{business.data?.name ?? ""}</p>
      <p role="status" className="mt-2 text-label text-muted">
        {said}
      </p>
      {removeError !== null && <Alert>{removeError}</Alert>}

      {!list.data ? (
        <DelayedSkeleton rows={3} />
      ) : (
        <>
          {list.data.length === 0 ? (
            <p className="mt-4 text-body text-muted">Nobody yet. Add the first person who will run the queue.</p>
          ) : (
            <ul className="mt-4 space-y-3">
              {list.data.map((person) => (
                <li key={person.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface p-4">
                  <div>
                    <h2 className="text-body-lg font-semibold">{person.name}</h2>
                    <p className="mt-1 text-label text-muted">{person.email ?? ""}</p>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={(event) => {
                      opener.current = event.currentTarget;
                      setRemoving(person);
                    }}
                  >
                    {`Remove ${person.name}`}
                  </Button>
                </li>
              ))}
            </ul>
          )}

          {adding ? (
            <Form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
              <h2 className="text-h2 font-semibold">Add a staff member</h2>
              <TextField
                id="staff-name"
                label="Name"
                value={form.name}
                maxLength={120}
                autoComplete="off"
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                error={invalid("name")}
              />
              <TextField
                id="staff-email"
                label="Email"
                type="email"
                value={form.email}
                autoComplete="off"
                onChange={(event) => setForm({ ...form, email: event.target.value })}
                error={invalid("email")}
              />
              <div>
                <TextField
                  id="staff-entry"
                  label="First password"
                  type={reveal ? "text" : "password"}
                  value={form.entry}
                  autoComplete="new-password"
                  onChange={(event) => setForm({ ...form, entry: event.target.value })}
                  error={invalid("entry")}
                />
                <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
                  <p className="text-label text-muted">
                    You choose it and tell them yourself; nobody can look it up afterwards.
                  </p>
                  <Button type="button" variant="outline" onClick={() => setReveal(!reveal)}>
                    {reveal ? "Hide" : "Show"}
                  </Button>
                </div>
              </div>
              {error !== null && serverFields.length === 0 && <Alert>{errorMessage(error)}</Alert>}
              {slow && busy && <p className="text-label text-muted">Still working… check the list.</p>}
              <div className="flex flex-wrap gap-2">
                <Button type="submit" disabled={busy && !slow} loading={busy && !slow}>
                  Add
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setAdding(false);
                    setForm(blankStaff());
                    setReveal(false);
                    forget();
                  }}
                >
                  Cancel
                </Button>
              </div>
            </Form>
          ) : (
            <Button type="button" className="mt-4" onClick={() => { setAdding(true); setProblems({}); forget(); }}>
              Add a staff member
            </Button>
          )}
        </>
      )}

      <dialog
        ref={dialog}
        aria-labelledby="remove-staff-title"
        onCancel={(event) => {
          event.preventDefault(); // Esc keeps them
          setRemoving(null);
        }}
        onClose={() => {
          setRemoving(null);
          returnFocus(opener.current, dialog.current);
        }}
        className="m-auto w-[calc(100%-2rem)] max-w-[420px] rounded-lg border border-border bg-surface p-6 text-text shadow-2 backdrop:bg-black/40"
      >
        <h2 id="remove-staff-title" className="text-h2 font-semibold">
          Remove this person?
        </h2>
        <p className="mt-2 text-body text-muted">
          {removing
            ? `${removing.name} loses access to this business at once, including any console they have open. Their account stays, and you cannot add the same email again, so only remove someone who has really left.`
            : ""}
        </p>
        <div className="mt-6 flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => setRemoving(null)}>
            Keep them
          </Button>
          <Button
            type="button"
            onClick={() => {
              const target = removing;
              setRemoving(null);
              if (target) void remove(target);
            }}
          >
            Remove them
          </Button>
        </div>
      </dialog>
    </section>
  );
}

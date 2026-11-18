"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Form } from "@/components/Form";
import { TextField } from "@/components/TextField";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { OpenHoursEditor } from "@/components/owner/OpenHoursEditor";
import { errorMessage, invalidFields } from "@/lib/api/errors";
import { hoursProblem, toApi, type OpenHours } from "@/lib/owner/hours";
import {
  BOUNDS,
  asSettingsForm,
  changedSettings,
  settingsProblems,
  settingsQuery,
  type Numeric,
  type OwnerBusiness,
  type SettingsForm,
} from "@/lib/owner/settings";
import { useKeyedWrite } from "@/lib/owner/useKeyedWrite";
import { nowIn, zoneNames } from "@/lib/owner/zones";

const FIELD_COPY: Record<string, string> = {
  name: "Check the name.",
  address: "Check the address.",
  timezone: "You can't change the time zone while appointments are active.",
  open_hours: "Check the opening hours.",
  settings: "Check the numbers below.",
};

/** Everything about one business the API lets an owner change: its name, address, time zone,
 *  opening hours, and the handful of queue and booking numbers `SettingsPatch` accepts. Only
 *  what the owner edited is sent, and the screen reads the business again afterwards. */
export function OwnerSettings({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient();
  const saved = useQuery({ ...settingsQuery(businessId), retry: false });
  const { run, busy, slow, error } = useKeyedWrite<OwnerBusiness>();

  // Only the owner's edits are state; every field reads through to what the server has, so a
  // save needs no copying back and a refetch cannot be out of step with the form.
  const [edits, setEdits] = useState<Partial<SettingsForm>>({});
  const [editedHours, setEditedHours] = useState<OpenHours | null>(null);
  const [problems, setProblems] = useState<Partial<Record<keyof SettingsForm, string>>>({});
  const [hoursError, setHoursError] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const zones = zoneNames(saved.data?.timezone);

  const form: SettingsForm | null = saved.data ? { ...asSettingsForm(saved.data), ...edits } : null;
  const hours: OpenHours = editedHours ?? ((saved.data?.open_hours ?? {}) as OpenHours);
  const hoursTouched = editedHours !== null;
  const setForm = (next: SettingsForm) => setEdits({ ...edits, ...next });

  const serverFields = invalidFields(error);
  const invalid = (field: keyof SettingsForm | "open_hours" | "settings") =>
    problems[field as keyof SettingsForm] ?? (serverFields.includes(field) ? FIELD_COPY[field] : undefined);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form || !saved.data) return;
    const wrong = settingsProblems(form);
    const wrongHours = hoursTouched ? hoursProblem(hours) : null;
    setProblems(wrong);
    setHoursError(wrongHours);
    if (Object.keys(wrong).length > 0 || wrongHours) {
      const first = (["name", "address", "timezone", ...(Object.keys(BOUNDS) as Numeric[]), "walk_ins", "appointments"] as const)
        .find((field) => wrong[field]);
      if (first) document.getElementById(`settings-${first}`)?.focus();
      return;
    }
    const patch = changedSettings(saved.data, form, hours, hoursTouched, toApi);
    if (Object.keys(patch).length === 0) {
      setSaid("Nothing to save");
      return;
    }
    const answer = await run(`/v1/businesses/${businessId}`, "PATCH", patch);
    if (answer === "failed") return;
    setEdits({}); // the server's answer is the truth now
    setEditedHours(null);
    setSaid("Saved");
    await queryClient.invalidateQueries({ queryKey: settingsQuery(businessId).queryKey });
  }

  if (saved.isError) {
    return (
      <section className="mx-auto max-w-[640px] px-4 py-8">
        <h1 className="text-h1 font-semibold">Settings</h1>
        <Alert>{errorMessage(saved.error)}</Alert>
        <Button type="button" className="mt-4" onClick={() => void saved.refetch()}>
          Retry
        </Button>
      </section>
    );
  }

  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Settings</h1>
      <p role="status" className="mt-2 text-label text-muted">
        {said}
      </p>
      {!form ? (
        <DelayedSkeleton rows={4} />
      ) : (
        <Form onSubmit={onSubmit} className="mt-4 space-y-4" noValidate>
          <TextField
            id="settings-name"
            label="Business name"
            value={form.name}
            maxLength={120}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            error={invalid("name")}
          />
          <TextField
            id="settings-address"
            label="Address (optional)"
            value={form.address}
            maxLength={320}
            onChange={(event) => setForm({ ...form, address: event.target.value })}
            error={invalid("address")}
          />
          <div>
            <label htmlFor="settings-timezone" className="block text-label font-medium">
              Time zone
            </label>
            <select
              id="settings-timezone"
              value={form.timezone}
              onChange={(event) => setForm({ ...form, timezone: event.target.value })}
              aria-invalid={serverFields.includes("timezone") ? true : undefined}
              aria-describedby="settings-timezone-hint"
              className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body"
            >
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
            <p id="settings-timezone-hint" className="mt-1 text-label text-muted">
              {`It is ${nowIn(form.timezone)} there now. It cannot change while an appointment is still to come.`}
            </p>
            {invalid("timezone") && <p className="mt-1 text-label text-danger">{invalid("timezone")}</p>}
          </div>

          <OpenHoursEditor
            hours={hours}
            onChange={setEditedHours}
            error={hoursError ?? invalid("open_hours")}
          />

          <fieldset>
            <legend className="text-label font-medium">Queue and booking</legend>
            <div className="mt-2 space-y-4">
              {(Object.keys(BOUNDS) as Numeric[]).map((field) => (
                <TextField
                  key={field}
                  id={`settings-${field}`}
                  label={BOUNDS[field].label}
                  inputMode="numeric"
                  value={form[field]}
                  onChange={(event) => setForm({ ...form, [field]: event.target.value })}
                  error={invalid(field)}
                />
              ))}
              <div className="flex flex-wrap gap-4">
                <TextField
                  id="settings-walk_ins"
                  label="Walk-ins per"
                  inputMode="numeric"
                  value={form.walk_ins}
                  onChange={(event) => setForm({ ...form, walk_ins: event.target.value })}
                  error={invalid("walk_ins")}
                />
                <TextField
                  id="settings-appointments"
                  label="appointments"
                  inputMode="numeric"
                  value={form.appointments}
                  onChange={(event) => setForm({ ...form, appointments: event.target.value })}
                  error={invalid("appointments")}
                />
              </div>
            </div>
          </fieldset>

          {error !== null && serverFields.length === 0 && <Alert>{errorMessage(error)}</Alert>}
          {slow && busy && <p className="text-label text-muted">Still working… check the settings.</p>}
          <Button type="submit" disabled={busy && !slow} loading={busy && !slow}>
            Save
          </Button>
        </Form>
      )}
    </section>
  );
}

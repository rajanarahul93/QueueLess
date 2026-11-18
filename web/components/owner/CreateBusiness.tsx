"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { Form } from "@/components/Form";
import { TextField } from "@/components/TextField";
import { OpenHoursEditor } from "@/components/owner/OpenHoursEditor";
import { myBusinessesQuery } from "@/components/console/BusinessPicker";
import { errorMessage, invalidFields } from "@/lib/api/errors";
import { defaultHours, hoursProblem, toApi, type OpenHours } from "@/lib/owner/hours";
import { browserZone, nowIn, zoneNames } from "@/lib/owner/zones";
import { useCreateBusiness } from "@/lib/owner/useCreateBusiness";

const FIELD_COPY: Record<string, string> = {
  name: "Give the business a name.",
  category: "Say what kind of business it is.",
  timezone: "Pick the time zone the business keeps.",
  address: "That address is too long.",
  open_hours: "Check the opening hours.",
};

export function CreateBusiness() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const zones = useMemo(() => zoneNames(), []);
  const [zone, setZone] = useState(() => browserZone());
  const [hours, setHours] = useState<OpenHours>(() => defaultHours());
  const [hoursError, setHoursError] = useState<string | null>(null);
  const { run, busy, slow, error } = useCreateBusiness();
  // Which fields the server rejected, from the problem body itself: no second copy to keep.
  const fields = invalidFields(error);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const wrong = hoursProblem(hours);
    setHoursError(wrong);
    if (wrong) return;
    const address = String(form.get("address") ?? "").trim();
    const created = await run({
      name: String(form.get("name") ?? "").trim(),
      category: String(form.get("category") ?? "").trim(),
      ...(address ? { address } : {}),
      timezone: zone,
      open_hours: toApi(hours),
    });
    if (created === "failed") return;
    // Nothing optimistic: the hub reads the list again and shows what the server has.
    await queryClient.invalidateQueries({ queryKey: myBusinessesQuery.queryKey });
    router.replace(`/owner?created=${encodeURIComponent(created.name)}`);
  }

  const invalid = (name: string) => (fields.includes(name) ? FIELD_COPY[name] : undefined);

  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Create a business</h1>
      <p className="mt-2 text-body text-muted">
        People find it by name, and every time it shows is the time zone you pick here.
      </p>
      <Form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
        <TextField id="name" name="name" label="Business name" required maxLength={100} error={invalid("name")} />
        <TextField
          id="category"
          name="category"
          label="Category"
          required
          maxLength={50}
          placeholder="Clinic, salon, workshop…"
          error={invalid("category")}
        />
        <TextField id="address" name="address" label="Address (optional)" maxLength={300} error={invalid("address")} />
        <div>
          <label htmlFor="timezone" className="block text-label font-medium">
            Time zone
          </label>
          <select
            id="timezone"
            name="timezone"
            value={zone}
            onChange={(event) => setZone(event.target.value)}
            aria-invalid={fields.includes("timezone") ? true : undefined}
            aria-describedby="timezone-hint"
            className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body"
          >
            {zones.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <p id="timezone-hint" className="mt-1 text-label text-muted">
            {`It is ${nowIn(zone)} there now. This starts as your own zone; change it if the business keeps another.`}
          </p>
          {invalid("timezone") && (
            <p className="mt-1 text-label text-danger">{invalid("timezone")}</p>
          )}
        </div>
        <OpenHoursEditor hours={hours} onChange={setHours} error={hoursError ?? invalid("open_hours")} />
        {error !== null && <Alert>{errorMessage(error)}</Alert>}
        {slow && busy && <p className="text-label text-muted">Still working… check your businesses.</p>}
        <Button type="submit" disabled={busy && !slow} loading={busy && !slow}>
          Create business
        </Button>
      </Form>
    </section>
  );
}

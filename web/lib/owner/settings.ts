import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { OpenHours } from "@/lib/owner/hours";

export type OwnerBusiness = components["schemas"]["BusinessOwnerOut"];

/** The settings the API really takes (`SettingsPatch` in `app/schemas/business.py`), with its
 *  own bounds. Anything not listed here is not editable through the API. */
export const BOUNDS = {
  grace_period_min: { min: 0, max: 60, label: "Grace period before a no-show (minutes)" },
  reschedule_cutoff_min: { min: 0, max: 10080, label: "Cannot move an appointment within (minutes)" },
  promotion_wait_limit_min: { min: 1, max: 240, label: "Promote after waiting (minutes)" },
  running_late_threshold_min: { min: 1, max: 120, label: "Call it running late after (minutes)" },
  max_walk_ins: { min: 0, max: 1000, label: "Walk-ins allowed in a day" },
} as const;
export const RATIO_BOUNDS = { min: 1, max: 10 };
export const MAX_NAME = 100;
export const MAX_ADDRESS = 300;

export type Numeric = keyof typeof BOUNDS;
export type SettingsForm = Record<Numeric, string> & {
  name: string;
  address: string;
  timezone: string;
  walk_ins: string;
  appointments: string;
};

export const settingsQuery = (businessId: string) => ({
  queryKey: ["owner-settings", businessId] as const,
  queryFn: () => api<OwnerBusiness>(`/v1/businesses/${businessId}/settings`),
});

const str = (value: unknown, fallback: string) => (typeof value === "number" ? String(value) : fallback);

export function asSettingsForm(business: OwnerBusiness): SettingsForm {
  const settings = (business.settings ?? {}) as Record<string, unknown>;
  const ratio = (settings.walk_in_ratio ?? {}) as Record<string, unknown>;
  return {
    name: business.name,
    address: business.address ?? "",
    timezone: business.timezone,
    grace_period_min: str(settings.grace_period_min, "5"),
    reschedule_cutoff_min: str(settings.reschedule_cutoff_min, "120"),
    promotion_wait_limit_min: str(settings.promotion_wait_limit_min, "20"),
    running_late_threshold_min: str(settings.running_late_threshold_min, "10"),
    max_walk_ins: str(settings.max_walk_ins, "0"),
    walk_ins: str(ratio.walk_ins, "1"),
    appointments: str(ratio.appointments, "2"),
  };
}

const whole = (value: string) => /^\d+$/.test(value.trim());

/** The first thing wrong with each field, in the owner's words. */
export function settingsProblems(form: SettingsForm): Partial<Record<keyof SettingsForm, string>> {
  const problems: Partial<Record<keyof SettingsForm, string>> = {};
  const name = form.name.trim();
  if (!name) problems.name = "The business needs a name.";
  else if (name.length > MAX_NAME) problems.name = `Keep the name to ${MAX_NAME} characters.`;
  if (form.address.trim().length > MAX_ADDRESS) problems.address = `Keep the address to ${MAX_ADDRESS} characters.`;
  if (!form.timezone) problems.timezone = "Pick the time zone the business keeps.";
  for (const [field, bound] of Object.entries(BOUNDS) as [Numeric, { min: number; max: number }][]) {
    const value = form[field];
    if (!whole(value) || Number(value) < bound.min || Number(value) > bound.max) {
      problems[field] = `A whole number from ${bound.min} to ${bound.max}.`;
    }
  }
  for (const field of ["walk_ins", "appointments"] as const) {
    const value = form[field];
    if (!whole(value) || Number(value) < RATIO_BOUNDS.min || Number(value) > RATIO_BOUNDS.max) {
      problems[field] = `A whole number from ${RATIO_BOUNDS.min} to ${RATIO_BOUNDS.max}.`;
    }
  }
  return problems;
}

type Patch = {
  name?: string;
  address?: string | null;
  timezone?: string;
  open_hours?: Record<string, { open: string; close: string }[]>;
  settings?: Record<string, number | { walk_ins: number; appointments: number }>;
};

/** Only what the owner changed. Owner writes have no version control, so a field they did not
 *  touch must not be sent: it would overwrite whatever another tab has just saved. The walk-in
 *  ratio is replaced as a whole, so both halves go together when either changes. */
export function changedSettings(
  before: OwnerBusiness,
  form: SettingsForm,
  hours: OpenHours,
  hoursTouched: boolean,
  toApiHours: (hours: OpenHours) => Record<string, { open: string; close: string }[]>,
): Patch {
  const was = asSettingsForm(before);
  const patch: Patch = {};
  if (form.name.trim() !== was.name) patch.name = form.name.trim();
  const address = form.address.trim();
  if (address !== was.address) patch.address = address === "" ? null : address; // null clears it
  if (form.timezone !== was.timezone) patch.timezone = form.timezone;
  if (hoursTouched) patch.open_hours = toApiHours(hours);
  const settings: Record<string, number | { walk_ins: number; appointments: number }> = {};
  for (const field of Object.keys(BOUNDS) as Numeric[]) {
    if (form[field].trim() !== was[field]) settings[field] = Number(form[field]);
  }
  if (form.walk_ins.trim() !== was.walk_ins || form.appointments.trim() !== was.appointments) {
    settings.walk_in_ratio = { walk_ins: Number(form.walk_ins), appointments: Number(form.appointments) };
  }
  if (Object.keys(settings).length > 0) patch.settings = settings;
  return patch;
}

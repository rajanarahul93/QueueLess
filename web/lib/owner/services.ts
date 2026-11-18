import { api } from "@/lib/api/client";
import type { Service } from "@/lib/api/types";

/** The server's own limits (`app/schemas/business.py`), mirrored so the owner is told before
 *  a press rather than after: name 1-100 after trimming, a duration of at least one minute,
 *  a buffer that may be zero, both at most a day, and a price of at most 10 digits with 2
 *  decimals. `price` is a STRING everywhere: the API sends `numeric(10,2)` as a string and
 *  takes one back, so no money ever passes through a float. */
export const MAX_NAME = 100;
export const MAX_MINUTES = 1440;
export const PRICE_PATTERN = /^\d{1,8}(\.\d{1,2})?$/;

export type ServiceForm = { name: string; duration_min: string; buffer_min: string; price: string };

export const servicesQuery = (businessId: string) => ({
  // The owner's own list, which is the only one that includes the inactive services.
  queryKey: ["owner-services", businessId] as const,
  queryFn: () => api<Service[]>(`/v1/businesses/${businessId}/services?include_inactive=true`),
});

export const asForm = (service: Service): ServiceForm => ({
  name: service.name,
  duration_min: String(service.duration_min),
  buffer_min: String(service.buffer_min),
  price: service.price,
});

export const blankForm = (): ServiceForm => ({ name: "", duration_min: "30", buffer_min: "0", price: "0.00" });

/** What is wrong with each field, in the owner's words, or nothing. */
export function formProblems(form: ServiceForm): Partial<Record<keyof ServiceForm, string>> {
  const problems: Partial<Record<keyof ServiceForm, string>> = {};
  const name = form.name.trim();
  if (!name) problems.name = "Give the service a name.";
  else if (name.length > MAX_NAME) problems.name = `Keep the name to ${MAX_NAME} characters.`;
  const duration = Number(form.duration_min);
  if (!/^\d+$/.test(form.duration_min.trim()) || duration < 1 || duration > MAX_MINUTES) {
    problems.duration_min = `A duration from 1 to ${MAX_MINUTES} minutes.`;
  }
  const buffer = Number(form.buffer_min);
  if (!/^\d+$/.test(form.buffer_min.trim()) || buffer < 0 || buffer > MAX_MINUTES) {
    problems.buffer_min = `A buffer from 0 to ${MAX_MINUTES} minutes.`;
  }
  if (!PRICE_PATTERN.test(form.price.trim())) problems.price = "A price like 250 or 250.00.";
  return problems;
}

/** The API shape of a whole service, for a create. */
export function toCreate(form: ServiceForm): { name: string; duration_min: number; buffer_min: number; price: string } {
  return {
    name: form.name.trim(),
    duration_min: Number(form.duration_min),
    buffer_min: Number(form.buffer_min),
    price: form.price.trim(),
  };
}

/** Only what the owner actually changed: owner writes have no version control, so sending a
 *  field the owner did not touch would overwrite whatever another tab has just saved. The
 *  price is compared as the string it is, never as a number. */
export function changedFields(before: Service, form: ServiceForm): Partial<{ name: string; duration_min: number; buffer_min: number; price: string }> {
  const next = toCreate(form);
  const patch: Partial<typeof next> = {};
  if (next.name !== before.name) patch.name = next.name;
  if (next.duration_min !== before.duration_min) patch.duration_min = next.duration_min;
  if (next.buffer_min !== before.buffer_min) patch.buffer_min = next.buffer_min;
  if (next.price !== before.price) patch.price = next.price;
  return patch;
}

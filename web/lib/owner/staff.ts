import { api } from "@/lib/api/client";
import type { Staff } from "@/lib/api/types";

export type { Staff };

/** The server's own limits (`app/schemas/business.py`, `app/schemas/auth.py`): a name of 1-100
 *  characters after trimming, an email, and a first password of 10-128 characters that the owner
 *  types. The list gives id, name and email only - no role and no joined date - so the screen
 *  shows what there is. */
export const MAX_NAME = 100;
export const MIN_PASSPHRASE = 10;
export const MAX_PASSPHRASE = 128;

export const staffQuery = (businessId: string) => ({
  queryKey: ["owner-staff", businessId] as const,
  queryFn: () => api<Staff[]>(`/v1/businesses/${businessId}/staff`),
});

export type StaffForm = { name: string; email: string; entry: string };

export const blankStaff = (): StaffForm => ({ name: "", email: "", entry: "" });

/** What is wrong with each field, in the owner's words, or nothing. */
export function staffProblems(form: StaffForm): Partial<Record<keyof StaffForm, string>> {
  const problems: Partial<Record<keyof StaffForm, string>> = {};
  const name = form.name.trim();
  if (!name) problems.name = "Give the person a name.";
  else if (name.length > MAX_NAME) problems.name = `Keep the name to ${MAX_NAME} characters.`;
  const email = form.email.trim();
  // The server validates the address properly; this only catches the obvious before a press.
  if (!email) problems.email = "An email address they will sign in with.";
  else if (!/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(email)) problems.email = "That does not look like an email address.";
  if (form.entry.length < MIN_PASSPHRASE) problems.entry = `At least ${MIN_PASSPHRASE} characters.`;
  else if (form.entry.length > MAX_PASSPHRASE) problems.entry = `At most ${MAX_PASSPHRASE} characters.`;
  return problems;
}

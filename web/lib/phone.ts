// The API accepts only strict E.164 and never normalizes, so the client builds it.
export const E164 = /^\+[1-9]\d{7,14}$/;

export const COUNTRY_CODES = [
  { code: "91", label: "India (+91)" },
  { code: "1", label: "United States / Canada (+1)" },
  { code: "44", label: "United Kingdom (+44)" },
  { code: "61", label: "Australia (+61)" },
  { code: "65", label: "Singapore (+65)" },
  { code: "971", label: "United Arab Emirates (+971)" },
] as const;

/** "+<country><number>" from what the user typed: spaces, dashes, brackets and a leading trunk
 *  0 are dropped. Null when the result is not valid E.164. */
export function toE164(countryCode: string, typed: string): string | null {
  const digits = typed.replace(/\D/g, "").replace(/^0+/, "");
  const phone = `+${countryCode}${digits}`;
  return E164.test(phone) ? phone : null;
}

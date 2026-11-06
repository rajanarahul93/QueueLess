/** The business a staff member last opened: a per-viewer convenience, this tab only. Storage can
 *  be unavailable (private windows, blocked site data), so every access is guarded and the app
 *  works without it. A stored id is trusted only when it is still in the user's own list. */
const NAME = "queueless-console-business";

export function rememberBusiness(id: string): void {
  try {
    sessionStorage.setItem(NAME, id);
  } catch {
    // no storage: the picker simply asks again next time
  }
}

export function rememberedBusiness(): string | null {
  try {
    return sessionStorage.getItem(NAME);
  } catch {
    return null;
  }
}

export function forgetBusiness(): void {
  try {
    sessionStorage.removeItem(NAME);
  } catch {
    // nothing stored, nothing to clear
  }
}

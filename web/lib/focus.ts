/** Focus back to the control that opened a dialog, unless focus has already moved somewhere
 *  real. A modal's `close` event is its own task, so this runs after React has committed
 *  whatever the confirmed action set - including a notice the caller focused - and an
 *  unconditional `invoker.focus()` would take focus off it. The platform applies the same rule
 *  to its own restore: closing a modal only moves focus when the dialog still holds it.
 *
 *  An opener the list has since removed needs no check of its own: focusing a detached element
 *  does nothing, in every browser and in jsdom. */
export function returnFocus(invoker: HTMLElement | null, dialog: HTMLElement | null): void {
  if (!invoker) return;
  const active = document.activeElement;
  const parked = active === null || active === document.body || dialog?.contains(active) === true;
  if (parked) invoker.focus();
}

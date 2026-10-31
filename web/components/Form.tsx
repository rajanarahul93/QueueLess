"use client";

import { useSyncExternalStore, type FormHTMLAttributes } from "react";

const subscribe = () => () => {};

/** A form that is inert until React has hydrated it. The server HTML renders its fields and
 *  buttons disabled (one fieldset), so nothing typed before then is lost to React's state and
 *  no click or Enter submits it natively (a GET would put the fields, a password too, in the URL). */
export function Form({ className = "", children, ...props }: FormHTMLAttributes<HTMLFormElement>) {
  const hydrated = useSyncExternalStore(subscribe, () => true, () => false);
  return (
    <form {...props}>
      <fieldset disabled={!hydrated} className={`min-w-0 ${className}`}>
        {children}
      </fieldset>
    </form>
  );
}

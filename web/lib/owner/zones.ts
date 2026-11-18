/** Every zone the browser knows, so the owner picks the business's own. The API checks the name
 *  against its own tzdata and answers 422 on `timezone` if it disagrees. `own` is the zone already
 *  stored: a browser that lists only the legacy alias ("Asia/Calcutta") would otherwise leave it
 *  without an option, and the select would quietly show the first zone in the list. */
export function zoneNames(own?: string): string[] {
  const supported = (Intl as { supportedValuesOf?: (key: "timeZone") => string[] }).supportedValuesOf;
  const names = supported ? supported("timeZone") : [];
  // The browser's own zone can be a name its list leaves out (an older build answers
  // "Asia/Calcutta" and lists only that, or the other way round), and a preselection that
  // matches no option would quietly select somebody else's zone.
  const all = new Set([...names, browserZone(), "UTC", own]);
  return [...all].filter((zone): zone is string => Boolean(zone)).sort();
}


export function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}


/** The time it is right now where the business is, so a wrong zone is visible before saving. */
export function nowIn(zone: string): string {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: zone, hour: "numeric", minute: "2-digit" }).format(new Date());
  } catch {
    return "—";
  }
}


// The rules of the per-route bundle check, kept apart from the file reading so they can be tested.
export const BUDGET_KB = 200; // initial JS of any customer route, gzipped
export const DRIFT_KB = 2; // any route moving more than this needs a reviewed baseline update

/** Public and customer pages: the ones a customer's phone loads. */
export function isCustomerRoute(route) {
  return route === "/" || ["/login", "/me", "/b"].some((p) => route === p || route.startsWith(`${p}/`));
}

/** sizes and baseline map route -> KB. Returns printable lines and the failures. */
export function evaluate(sizes, baseline) {
  const lines = [];
  const failures = [];
  for (const route of Object.keys(sizes).sort()) {
    const kb = sizes[route];
    const before = baseline[route];
    const delta = before === undefined ? "new" : `${kb - before >= 0 ? "+" : ""}${(kb - before).toFixed(1)}`;
    const customer = isCustomerRoute(route);
    lines.push(`${route.padEnd(24)} ${kb.toFixed(1).padStart(6)} KB  (${delta} vs baseline)${customer ? "  customer" : ""}`);
    if (customer && kb > BUDGET_KB) failures.push(`${route} is ${kb.toFixed(1)} KB, over the ${BUDGET_KB} KB budget`);
    if (before === undefined) failures.push(`${route} is not in the baseline (add it with --write-baseline)`);
    else if (Math.abs(kb - before) > DRIFT_KB) {
      failures.push(`${route} moved ${delta} KB (more than ${DRIFT_KB}): update the baseline in a reviewed change`);
    }
  }
  for (const route of Object.keys(baseline)) {
    if (!(route in sizes)) failures.push(`${route} is in the baseline but no longer built`);
  }
  return { lines, failures };
}

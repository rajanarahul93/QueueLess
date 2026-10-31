export const BUDGET_KB: number;
export const DRIFT_KB: number;
export function isCustomerRoute(route: string): boolean;
export function evaluate(
  sizes: Record<string, number>,
  baseline: Record<string, number>,
): { lines: string[]; failures: string[] };

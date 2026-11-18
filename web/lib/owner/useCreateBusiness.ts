"use client";

import { useCallback } from "react";
import { useKeyedWrite } from "@/lib/owner/useKeyedWrite";
import type { Business } from "@/lib/api/types";

export { SLOW_PRESS_MS } from "@/lib/owner/useKeyedWrite";

/** What the owner filled in, in the shape the API takes. */
export type NewBusiness = {
  name: string;
  category: string;
  address?: string;
  timezone: string;
  open_hours: Record<string, { open: string; close: string }[]>;
};

/** Creating a business: the owner write rules of `useKeyedWrite`, on one endpoint. */
export function useCreateBusiness() {
  const { run, busy, slow, error } = useKeyedWrite<Business>();
  const create = useCallback((body: NewBusiness) => run("/v1/businesses", "POST", body), [run]);
  return { run: create, busy, slow, error };
}

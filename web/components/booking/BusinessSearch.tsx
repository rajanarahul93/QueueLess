"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, type FormEvent } from "react";
import { Alert } from "@/components/Alert";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api, ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { BusinessPage } from "@/lib/api/types";
import { useCountdown } from "@/lib/useCountdown";

function listUrl(q: string, category: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: "20" });
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  if (cursor) params.set("cursor", cursor);
  return `/v1/businesses?${params}`;
}

export function BusinessSearch() {
  const router = useRouter();
  const params = useSearchParams();
  const q = params.get("q") ?? "";
  const category = params.get("category") ?? "";
  const [wait, startWait] = useCountdown();
  const results = useInfiniteQuery({
    queryKey: ["businesses", q, category],
    queryFn: ({ pageParam }) =>
      api<BusinessPage>(listUrl(q, category, pageParam), { auth: false }),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    retry: false,
    staleTime: 60_000,
  });
  const limited = results.error instanceof ApiError && results.error.code === "RATE_LIMITED";

  useEffect(() => {
    if (results.error instanceof ApiError && results.error.retryAfterSec) {
      startWait(results.error.retryAfterSec);
    }
  }, [results.error, startWait]);

  function onSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    const text = String(form.get("q") ?? "").trim();
    const kind = String(form.get("category") ?? "").trim();
    if (text) next.set("q", text);
    if (kind) next.set("category", kind);
    router.replace(`/b${next.size ? `?${next}` : ""}`);
  }

  const items = results.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <>
      <form role="search" onSubmit={onSearch} className="mt-6 space-y-3">
        <div>
          <label htmlFor="q" className="block text-label font-medium">
            Name
          </label>
          <input id="q" name="q" type="search" defaultValue={q} maxLength={100}
            className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body" />
        </div>
        <div>
          <label htmlFor="category" className="block text-label font-medium">
            Category
          </label>
          <input id="category" name="category" defaultValue={category} maxLength={50}
            className="mt-1 block min-h-12 w-full rounded-sm border border-border-strong bg-surface px-3 text-body" />
        </div>
        <button type="submit" className="min-h-12 w-full rounded-md bg-primary px-4 text-label font-medium text-primary-fg">
          Search
        </button>
      </form>

      <div className="mt-8" aria-live="polite">
        {results.isPending && <DelayedSkeleton rows={3} />}
        {results.isError && (
          <div>
            <Alert>
              {limited && wait > 0
                ? `Too many searches from your network. Try again in ${wait} s.`
                : errorMessage(results.error)}
            </Alert>
            <button type="button" disabled={wait > 0} onClick={() => void results.refetch()}
              className="mt-3 min-h-11 text-label text-primary underline disabled:text-muted disabled:no-underline">
              Try again
            </button>
          </div>
        )}
        {results.isSuccess && (
          <>
            <p className="text-label text-muted">
              {items.length === 0 ? "No businesses match your search." : `${items.length} shown`}
            </p>
            <ul className="mt-3 space-y-2">
              {items.map((business) => (
                <li key={business.id}>
                  <Link href={`/b/${business.id}`}
                    className="block rounded-md border border-border bg-surface p-4 shadow-1">
                    <span className="block text-body-lg font-semibold">{business.name}</span>
                    <span className="block text-label text-muted">
                      {business.category}
                      {business.address ? ` · ${business.address}` : ""}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
            {results.hasNextPage && (
              <button type="button" onClick={() => void results.fetchNextPage()} disabled={results.isFetchingNextPage}
                className="mt-4 min-h-12 w-full rounded-md border border-border-strong px-4 text-label font-medium">
                {results.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            )}
          </>
        )}
      </div>
    </>
  );
}

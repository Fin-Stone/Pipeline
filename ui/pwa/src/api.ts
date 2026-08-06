/**
 * The only way this client reaches the server.
 *
 * Read ../../../docs/api-contracts.md before changing anything here. Two rules
 * from architecture §5.2 are load-bearing:
 *
 *  - **No endpoint is hardcoded.** The server address is the user's, entered at
 *    sign-in and stored per profile, exactly as Bitwarden does it. A build that
 *    bakes in a URL cannot be self-hosted.
 *  - **Nothing is imported from the Python side.** This client talks HTTP and
 *    shares nothing else, so it can be pointed at any server speaking the same
 *    API version.
 */

const SERVER_KEY = "finstone.serverUrl";
const PROFILE_KEY = "finstone.profile";

/** The contract this client is written against. */
export const REQUIRED_API_VERSION = "v1";

export function serverUrl(): string | null {
  return localStorage.getItem(SERVER_KEY);
}

export function setServer(url: string, profile: string): void {
  localStorage.setItem(SERVER_KEY, url.replace(/\/+$/, ""));
  localStorage.setItem(PROFILE_KEY, profile);
}

export function profile(): string {
  return localStorage.getItem(PROFILE_KEY) ?? "prod";
}

export function forgetServer(): void {
  localStorage.removeItem(SERVER_KEY);
  localStorage.removeItem(PROFILE_KEY);
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: unknown) {
    super(`API ${status}`);
  }
}

// `object` rather than Record<string, unknown>: a typed filter interface has no
// index signature, and widening one to get it would give up the checking that
// makes the filters worth typing at all.
async function call<T>(path: string, params: object = {}, init?: RequestInit): Promise<T> {
  const base = serverUrl();
  if (!base) throw new ApiError(0, "no server configured");

  const url = new URL(`${base}/api/${REQUIRED_API_VERSION}${path}`);
  url.searchParams.set("profile", profile());
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    // Repeatable filters: account_id and category may appear more than once.
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, String(v)));
    else url.searchParams.append(key, String(value));
  }

  const response = await fetch(url, init);
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try { detail = (await response.json()).detail ?? detail; } catch { /* body was not JSON */ }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------- shapes ---
// Every monetary field ends in _minor and is a whole number of cents. Spending
// is negative. Neither is converted here; see money.ts for why.

export interface Health { status: string; api_version: string }
export interface Category { id: number; name: string; position: number }
export interface Account {
  id: number; institution: string; account_ref_masked: string;
  sub_account_label: string | null; currency: string; kind: string;
}
export interface Summary {
  currency: string;
  range: { since: string | null; until: string | null; days: number | null };
  total_minor: number;
  by_category: { category: string | null; rows: number; total_minor: number }[];
  /** Null on an open-ended range: an average over unbounded time is not a number. */
  average_minor: { per_day: number | null; per_week: number | null; per_month: number | null };
}
export interface Series {
  merchant: string; amount_centre_minor: number; monthly_equivalent_minor: number;
  period_label: string; occurrences: number; total_paid_minor: number;
  first_seen: string; last_seen: string; expected_next: string; confidence: number;
  price_changes: { on: string; from_minor: number; to_minor: number }[];
}
export interface Recurring {
  currency: string; monthly_commitment_minor: number;
  series: Series[]; due_soon: Series[]; overdue: Series[]; lapsed: Series[];
}
export interface ReviewItem { counterparty: string; occurrences: number; total_minor: number }
export interface Review { outstanding: number; value_at_stake_minor: number; items: ReviewItem[] }

export type Direction = "out" | "in" | "net";

export interface TrendPoint {
  period: string; rows: number;
  out_minor: number; in_minor: number; net_minor: number;
  total_minor: number;
  rolling: { out_minor: number; in_minor: number; net_minor: number };
  /** How many buckets this point's average actually covered. Below the full
   *  window the line is still settling and should be marked as such. */
  rolling_of: number;
}
export interface Trend {
  currency: string; bucket: "day" | "week" | "month";
  rolling_window: number;
  range: { since: string | null; until: string | null; days: number | null };
  points: TrendPoint[];
  /** Both, because the gap between them is the information: a mean well below
   *  the median is being carried by a few large one-offs. Draw the line at the
   *  median. */
  centre: { mean_minor: number | null; median_minor: number | null; buckets: number };
}
export interface Txn {
  id: number; posted_date: string; amount_minor: number; currency: string;
  counterparty_norm: string; account_id: number; institution: string;
  account_ref_masked: string; category: string | null; source: string | null;
}
export interface HiddenRow {
  id: number; posted_date: string; amount_minor: number;
  counterparty_norm: string; institution: string; note: string; hidden_at: string;
}
export interface Hidden { hidden: HiddenRow[]; count: number; total_minor: number }

export interface GrowthPoint { on: string; total_minor: number; accounts_known: number }
export interface Growth {
  currency: string; window_months: number; since: string;
  points: GrowthPoint[];
  change: {
    from_minor: number | null; to_minor: number | null;
    change_minor: number | null;
    /** Null where the window opened at zero or in debt: there is no honest
     *  percentage of that, and this number carries a feeling. */
    percent: number | null;
  };
}

export interface Filters {
  since?: string; until?: string; account_id?: number[]; category?: string[];
  /** Hidden for this request only. The server owns the arithmetic, so an
   *  exclusion has to reach it or the totals describe a different set of
   *  transactions from the one on screen. */
  exclude_txn_id?: number[];
}

export const api = {
  health: () => call<Health>("/health"),
  accounts: () => call<{ accounts: Account[] }>("/accounts"),
  categories: () => call<{ categories: Category[] }>("/categories"),
  summary: (f: Filters, direction: Direction = "out") =>
    call<Summary>("/summary", { ...f, direction }),
  trend: (f: Filters) => call<Trend>("/trend", f),
  transactions: (f: Filters, limit = 100, direction: Direction = "out") =>
    call<{ transactions: Txn[] }>("/transactions", { ...f, limit, direction }),
  hidden: () => call<Hidden>("/hidden"),
  hide: (txn_id: number, note = "") =>
    call<{ hidden: boolean }>("/hidden", { txn_id, note }, { method: "POST" }),
  markTransfer: (txn_id: number) =>
    call<{ marked: boolean }>("/transfers/mark", { txn_id }, { method: "POST" }),
  unhide: (txn_id: number) =>
    call<{ restored: boolean }>(`/hidden/${txn_id}`, {}, { method: "DELETE" }),
  recurring: () => call<Recurring>("/recurring"),
  review: (limit = 50) => call<Review>("/review", { limit }),
  transfers: () => call<{ linked: number }>("/transfers"),
  growth: (months: number) => call<Growth>("/growth", { months }),
  addCategory: (name: string) =>
    call<{ created: boolean }>("/categories", { name }, { method: "POST" }),
  setCategory: (txn_id: number, category: string) =>
    call<{ category: string }>(
      `/transactions/${txn_id}/category`, { category }, { method: "POST" },
    ),
  clearCategory: (txn_id: number) =>
    call<{ cleared: boolean }>(
      `/transactions/${txn_id}/category`, {}, { method: "DELETE" },
    ),
  decide: (counterparty: string, category: string) =>
    call<{ created: boolean }>("/review/decide", { counterparty, category }, { method: "POST" }),
};

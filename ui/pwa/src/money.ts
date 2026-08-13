/**
 * Money crosses the wire as integer minor units and is converted exactly once,
 * here, for display.
 *
 * Nothing in this client does arithmetic on a converted value. Adding two
 * formatted numbers, or summing floats and formatting the result, reintroduces
 * precisely the error the ledger is built to avoid — and it would do so on the
 * screen the user trusts. Sum in minor units, format last.
 */

/** Spending arrives negative, signed by its effect on the account. */
export const isSpend = (minor: number) => minor < 0;

/**
 * What the ledger on the other end is denominated in.
 *
 * Learned from the server, not compiled in. Every endpoint that reports money
 * says which money it is, and `api.ts` hands that here as the responses arrive
 * — so the formatter follows the ledger instead of assuming the currency of
 * the household this was first built for. The initial value is only what is
 * shown before the first response lands.
 *
 * A ledger holding more than one currency is refused by the server rather than
 * summed, so there is never a second answer to hold.
 */
let ledger = "SGD";

export function setCurrency(code: string | null | undefined): void {
  if (code) ledger = code;
}

export const currency = () => ledger;

export function money(minor: number | null | undefined, currency = ledger): string {
  // Null is not zero. An average over an unbounded range has no value, and
  // showing 0.00 would state something false rather than admit a gap.
  if (minor === null || minor === undefined) return "—";
  return new Intl.NumberFormat("en-SG", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(minor / 100);
}

/** Magnitude only, for places where the sign is already carried by the label. */
export function magnitude(minor: number | null | undefined, currency = ledger): string {
  if (minor === null || minor === undefined) return "—";
  return money(Math.abs(minor), currency);
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

/** ISO date for an input[type=date], N months back from today. */
export function monthsAgo(months: number): string {
  const d = new Date();
  d.setMonth(d.getMonth() - months);
  return d.toISOString().slice(0, 10);
}

export const today = () => new Date().toISOString().slice(0, 10);

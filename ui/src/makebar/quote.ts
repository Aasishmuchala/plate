// Quote before spend (doctrine 8), the Make bar's side (T35). A quote lives 15 minutes; the bar counts down from
// the moment it arrived using the daemon's own lifetime (expires_utc - created_utc), so a skewed browser clock
// cannot keep a stale quote alive. Make is enabled only while the quote is fresh. A best-of-N group is N sibling
// runs under one quote; its total is what the auto cap is checked against: at or over the cap a human approves it
// in a dialog (the daemon's budgets.authorize uses the same "not strictly below the cap" test). A cross-engine
// comparison (`quote_many`, T102) is the same: one group, each engine at its own price, approved once against the
// sum; the group lives as long as its shortest-lived quote. The price tier a quote was priced at (`price_tier`,
// T84) and an `estimated` price (derived, not billed yet) are shown next to the price.

import type { CustodyGrade } from "../nodeTypes";
import type { QuoteWire, RunWire } from "../canvas/runTypes";

/** `models.QUOTE_TTL`. */
export const QUOTE_TTL_MS = 15 * 60 * 1000;
/** `budgets.Policy.per_run_auto_cap_usd` default; a quote response may carry the daemon's own value. */
export const DEFAULT_AUTO_CAP_USD = 2.0;

export interface QuoteState {
  /** The quote of the first run; siblings share its terms and expiry. */
  quote: QuoteWire;
  /** Every run under the quote, sibling order (one for N = 1). */
  runs: RunWire[];
  totalUsd: number;
  /** Browser clock (ms) when the quote arrived. */
  receivedAt: number;
  /** Lifetime of the quote in ms, from the daemon's own timestamps (capped at QUOTE_TTL_MS). */
  ttlMs: number;
  autoCapUsd: number;
  /** Set for a cross-engine comparison: one line per engine, in sibling order. */
  group?: GroupLine[];
}

/** One engine of a compared group: its run and the terms it was quoted at. */
export interface GroupLine {
  runId: string;
  engineId: string;
  provider: string;
  model: string;
  priceUsd: number;
  custody: CustodyGrade;
  etaS: number;
  priceStatus?: string;
  priceTier?: string | null;
}

export const CUSTODY_TONE: Readonly<Record<CustodyGrade, "green" | "blue" | "amber" | "red">> = {
  C0: "green",
  C1: "blue",
  C2: "amber",
  D: "red",
};

/** Lifetime of a quote from its own timestamps; falls back to the TTL when they are missing or nonsense. */
export function quoteLifetimeMs(q: Pick<QuoteWire, "created_utc" | "expires_utc">): number {
  const created = Date.parse(q.created_utc);
  const expires = Date.parse(q.expires_utc);
  if (!Number.isFinite(created) || !Number.isFinite(expires) || expires <= created) return QUOTE_TTL_MS;
  return Math.min(expires - created, QUOTE_TTL_MS);
}

/** Milliseconds left on a quote (0 when expired). */
export function remainingMs(state: Pick<QuoteState, "receivedAt" | "ttlMs">, now: number): number {
  return Math.max(0, state.receivedAt + state.ttlMs - now);
}

export const isFresh = (state: Pick<QuoteState, "receivedAt" | "ttlMs"> | null, now: number): boolean =>
  state !== null && remainingMs(state, now) > 0;

/** `m:ss`. */
export function formatCountdown(ms: number): string {
  const s = Math.ceil(Math.max(0, ms) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function formatUsd(usd: number): string {
  if (!Number.isFinite(usd)) return "n/a";
  return `${usd < 0.1 && usd > 0 ? usd.toFixed(3) : usd.toFixed(2)} USD`;
}

export function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "now";
  if (seconds < 90) return `~${Math.round(seconds)} s`;
  return `~${Math.round(seconds / 60)} min`;
}

/** At or over the cap needs a human: the same test the daemon applies. */
export const needsHumanApproval = (totalUsd: number, capUsd: number): boolean => totalUsd >= capUsd - 1e-9;

/** Sum of the siblings' quoted prices (each sibling is quoted at the full single price). */
export function totalPrice(runs: readonly RunWire[], fallback: QuoteWire): number {
  const prices = runs.map((r) => r.quote?.price_usd).filter((p): p is number => typeof p === "number");
  if (prices.length === runs.length && prices.length > 0) return prices.reduce((a, b) => a + b, 0);
  return fallback.price_usd * Math.max(1, runs.length);
}

/** The auto cap the daemon reported with the quote, if it did, else the default. */
export function autoCapFrom(body: unknown): number {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const direct = b["auto_cap_usd"];
    if (typeof direct === "number" && direct >= 0) return direct;
    const policy = b["policy"];
    if (policy && typeof policy === "object") {
      const cap = (policy as Record<string, unknown>)["per_run_auto_cap_usd"];
      if (typeof cap === "number" && cap >= 0) return cap;
    }
  }
  return DEFAULT_AUTO_CAP_USD;
}

// ---------------------------------------------------------------------------------------------------- T102

/** The manifest price tier a quote was priced at (`Quote.price_tier`, T84), when the daemon sent one. */
export function tierOf(q: QuoteWire | null | undefined): string | null {
  const tier = (q as (QuoteWire & { price_tier?: unknown }) | null | undefined)?.price_tier;
  return typeof tier === "string" && tier !== "" ? tier : null;
}

/** `quality=high,size=2K` -> `quality high · size 2K`; null when the quote was not priced from a tier. */
export function formatTier(tier: string | null | undefined): string | null {
  if (typeof tier !== "string" || tier.trim() === "") return null;
  return tier
    .split(",")
    .map((part) => part.trim().replace("=", " "))
    .filter(Boolean)
    .join(" · ");
}

/** The tone of a price status chip: `estimated` is amber (a derived figure, not a vendor's), others are dim. */
export const priceStatusTone = (status: string | null | undefined): "amber" | "dim" => (status === "estimated" ? "amber" : "dim");

/** `$0.034` / `$1.20`: a card's mean cost, compact enough for an engine option. */
export function formatMeanUsd(usd: number): string {
  if (!Number.isFinite(usd)) return "$n/a";
  return `$${usd < 0.1 && usd > 0 ? usd.toFixed(3) : usd.toFixed(2)}`;
}

/** `≥ 70% (20) · $0.034`: an engine card's Wilson lower bound, its run count and its mean cost. */
export function formatCard(card: { wilson_lower_95: number; n: number; mean_cost: number | null }): string {
  const pct = Math.round(Math.max(0, Math.min(1, card.wilson_lower_95)) * 100);
  const parts = [`≥ ${pct}% (${card.n})`];
  if (card.mean_cost !== null && Number.isFinite(card.mean_cost)) parts.push(formatMeanUsd(card.mean_cost));
  return parts.join(" · ");
}

const GRADE_RANK: Readonly<Record<CustodyGrade, number>> = { C0: 0, C1: 1, C2: 2, D: 3 };

/** The least private grade of a group: what approving all of it hands to vendors. */
export function worstGrade(grades: readonly CustodyGrade[]): CustodyGrade {
  return grades.reduce<CustodyGrade>((w, g) => (GRADE_RANK[g] > GRADE_RANK[w] ? g : w), "C0");
}

/** The group's lines from its runs; a run without a quote is priced at 0 and graded D (it says nothing better). */
export function groupLines(runs: readonly RunWire[], engineOf: (r: RunWire) => string): GroupLine[] {
  return runs.map((r) => ({
    runId: r.id,
    engineId: engineOf(r),
    provider: r.quote?.provider ?? "unknown",
    model: r.quote?.model ?? engineOf(r),
    priceUsd: typeof r.quote?.price_usd === "number" ? r.quote.price_usd : 0,
    custody: r.quote?.custody_grade ?? "D",
    etaS: typeof r.quote?.eta_s === "number" ? r.quote.eta_s : 0,
    priceStatus: r.quote?.price_status,
    priceTier: tierOf(r.quote),
  }));
}

/** A group lives as long as its shortest-lived quote: approving after that would approve a lapsed price. */
export function groupLifetimeMs(runs: readonly RunWire[]): number {
  const lives = runs.map((r) => (r.quote ? quoteLifetimeMs(r.quote) : QUOTE_TTL_MS));
  return lives.length > 0 ? Math.min(...lives) : QUOTE_TTL_MS;
}

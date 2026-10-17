// Small display formatters for the panels. Pure, so the tests pin the text.

/** `412.04` -> "412", `0.4567` -> "0.4567", `0` -> "0"; null / non-finite -> "n/a". Four significant digits. */
export function formatValue(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  if (v === 0) return "0";
  const abs = Math.abs(v);
  if (abs >= 1000) return String(Math.round(v));
  return String(Number(v.toPrecision(4)));
}

/** A threshold as the bound the value is held to: `<= 1`, `>= 0.9`, `= 0`, `0.4 to 0.6`; unknown -> an em dash. */
export function formatThreshold(t: unknown): string {
  if (t === null || t === undefined) return "—";
  if (typeof t === "string") return t;
  if (typeof t === "number") return formatValue(t);
  if (typeof t !== "object" || Array.isArray(t)) return "—";
  const o = t as Record<string, unknown>;
  const unit = typeof o.unit === "string" && o.unit !== "" ? ` ${o.unit}` : "";
  if (Array.isArray(o.range) && o.range.length === 2 && o.range.every((x) => typeof x === "number")) {
    return `${formatValue(o.range[0] as number)} to ${formatValue(o.range[1] as number)}${unit}`;
  }
  if (typeof o.value === "number") {
    const sym = o.op === "le" ? "<=" : o.op === "ge" ? ">=" : o.op === "eq" ? "=" : "";
    return `${sym ? `${sym} ` : ""}${formatValue(o.value)}${unit}`;
  }
  return "—";
}

export function formatUsd(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : `$${v.toFixed(2)}`;
}

/** `2026-10-17T14:05:09Z` -> "2026-10-17 14:05 UTC": stable across machines, no locale. */
export function formatTime(iso: string | undefined | null): string {
  if (!iso) return "—";
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]} UTC` : iso;
}

export function formatBytes(n: number | undefined | null): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatMinutes(m: number): string {
  return `${m.toFixed(1)} min`;
}

/** First 8 characters of an id or hash. */
export const shortId = (id: string): string => (id.length > 8 ? id.slice(0, 8) : id);

/** mm:ss for a running timer. */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

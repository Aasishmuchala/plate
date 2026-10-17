// One dark theme for the panels (the MaxGaffer palette from the T35 spec). Inline styles, as in the viewer: the
// stylesheet is owned by other tasks and the panels must look right without it.

import type { CSSProperties } from "react";
import type { CustodyGrade, GateStatus } from "./types";

export const C = {
  bg: "#1c1c1e",
  panel: "#2c2c2e",
  raised: "#3a3a3c",
  line: "#48484a",
  text: "#f2f2f7",
  muted: "#aeaeb2",
  accent: "#0a84ff",
  pass: "#30d158",
  fail: "#ff453a",
  advisory: "#ffd60a",
} as const;

export const STATUS_COLOR: Record<GateStatus, string> = { pass: C.pass, fail: C.fail, advisory: C.advisory };

/** C0 green, C1 blue, C2 amber, D red (T35 spec). */
export const GRADE_COLOR: Record<CustodyGrade, string> = { C0: C.pass, C1: C.accent, C2: C.advisory, D: C.fail };

export const S = {
  root: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    padding: 8,
    background: C.bg,
    color: C.text,
    fontSize: 12,
    borderTop: `1px solid ${C.line}`,
  },
  tabs: { display: "flex", flexWrap: "wrap", gap: 4 },
  tab: {
    padding: "3px 8px",
    background: C.panel,
    color: C.muted,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    cursor: "pointer",
    font: "inherit",
  },
  tabOn: { background: C.accent, color: "#fff", borderColor: C.accent },
  section: { display: "flex", flexDirection: "column", gap: 6, padding: 8, background: C.panel, borderRadius: 6 },
  h: { margin: 0, fontSize: 11, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: C.muted },
  row: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  list: { listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 },
  item: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
    padding: "4px 6px",
    background: C.bg,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    color: C.text,
    textAlign: "left",
    font: "inherit",
    cursor: "pointer",
  },
  itemOn: { borderColor: C.accent },
  chip: {
    display: "inline-block",
    padding: "0 6px",
    borderRadius: 8,
    border: `1px solid ${C.line}`,
    color: C.muted,
    fontSize: 11,
    lineHeight: "16px",
  },
  btn: {
    padding: "3px 8px",
    background: C.raised,
    color: C.text,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    cursor: "pointer",
    font: "inherit",
  },
  btnPrimary: { background: C.accent, borderColor: C.accent, color: "#fff" },
  input: {
    padding: "3px 6px",
    background: C.bg,
    color: C.text,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    font: "inherit",
    minWidth: 0,
  },
  muted: { color: C.muted },
  error: { color: C.fail, margin: 0 },
  mono: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" },
} satisfies Record<string, CSSProperties>;

/** A chip tinted with `color` (status, custody grade). The text names the state too: colour is never the only cue. */
export const tint = (color: string): CSSProperties => ({ ...S.chip, color, borderColor: color });

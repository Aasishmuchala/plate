// One dark look for the node editors: the panels' MaxGaffer palette, inline styles (the stylesheet belongs to other
// tasks and the editors must look right without it).

import type { CSSProperties } from "react";
import { C } from "../panels/theme";

export { C };

export type Tone = "pass" | "fail" | "advisory" | "info" | "muted";

export const TONE_COLOR: Readonly<Record<Tone, string>> = {
  pass: C.pass,
  fail: C.fail,
  advisory: C.advisory,
  info: C.accent,
  muted: C.muted,
};

const input: CSSProperties = {
  font: "inherit",
  color: C.text,
  background: C.bg,
  border: `1px solid ${C.line}`,
  borderRadius: 4,
  padding: "2px 6px",
  minWidth: 0,
};

export const E = {
  frame: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    padding: 8,
    background: C.bg,
    color: C.text,
    fontSize: 12,
    borderTop: `1px solid ${C.line}`,
  },
  head: { display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 },
  title: { margin: 0, fontSize: 13, fontWeight: 600 },
  sub: { color: C.muted, fontSize: 11 },
  section: { display: "flex", flexDirection: "column", gap: 6, padding: 8, background: C.panel, borderRadius: 6 },
  h: { margin: 0, fontSize: 11, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: C.muted },
  row: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  field: { display: "flex", alignItems: "center", gap: 6 },
  label: { color: C.muted, minWidth: 64 },
  input,
  number: { ...input, width: 72 },
  wide: { ...input, flex: 1 },
  button: {
    font: "inherit",
    color: C.text,
    background: C.raised,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    padding: "3px 8px",
    cursor: "pointer",
  },
  buttonOn: { background: C.accent, borderColor: C.accent, color: "#fff" },
  buttonOff: { opacity: 0.45, cursor: "not-allowed" },
  note: { color: C.muted, fontSize: 11 },
  error: { color: C.fail, fontSize: 11 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 11 },
  th: { textAlign: "left", color: C.muted, fontWeight: 500, padding: "2px 4px", borderBottom: `1px solid ${C.line}` },
  td: { padding: "2px 4px", borderBottom: `1px solid ${C.line}` },
  stage: { position: "relative", width: "100%", background: C.panel, overflow: "hidden", touchAction: "none", userSelect: "none" },
  overlay: { position: "absolute", inset: 0, pointerEvents: "none" },
} as const satisfies Record<string, CSSProperties>;

export function chipStyle(tone: Tone): CSSProperties {
  const color = TONE_COLOR[tone];
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "1px 7px",
    borderRadius: 10,
    border: `1px solid ${color}`,
    color,
    fontSize: 11,
    lineHeight: "16px",
    whiteSpace: "nowrap",
  };
}

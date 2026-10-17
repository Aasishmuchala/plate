// Reason codes for Fix and Render-it verdicts. A copy of calib/reason_codes.yaml (a test keeps the two equal): A and F
// are the Lensbook repair codes, P are PLATE's own gate codes. A code is never renumbered or reused.

export const REASON_GROUPS: Readonly<Record<string, string>> = {
  A: "Lensbook A-codes (stills)",
  F: "Lensbook F-codes (film)",
  P: "PLATE gate codes",
};

export const REASON_CODES: Readonly<Record<string, string>> = {
  A1: "geometry warp",
  A2: "texture crawl",
  A3: "scale drift",
  A4: "vegetation slop",
  A5: "re-cladding drift",
  F2: "slideshow",
  F4: "action ignored",
  F6: "camera improvises",
  F7: "continuity",
  F9: "extra people or objects",
  F11: "text or logos",
  F12: "light shift",
  P1: "registration",
  P2: "invented edge",
  P3: "colour shift",
  P4: "texture band",
  P5: "tone band",
  P6: "outside-mask leak",
  P7: "people implausible",
};

/** Codes grouped by their first letter, in file order. */
export function reasonGroups(): { group: string; title: string; codes: { code: string; text: string }[] }[] {
  const out: { group: string; title: string; codes: { code: string; text: string }[] }[] = [];
  for (const [code, text] of Object.entries(REASON_CODES)) {
    const group = code.slice(0, 1);
    let g = out.find((x) => x.group === group);
    if (!g) {
      g = { group, title: REASON_GROUPS[group] ?? group, codes: [] };
      out.push(g);
    }
    g.codes.push({ code, text });
  }
  return out;
}

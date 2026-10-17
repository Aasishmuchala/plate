// Verdict keys and the full-scrub gate. `1` Ship, `2` Fix, `3` Render-it act on the selected candidate. A verdict (and
// the later accept) needs a logged full scrub: the "Full scrub" box enables only after the viewer has shown all four
// quadrants of the image at 100% or more, and ticking it logs the scrub with the daemon. Fix and Render-it then ask
// for reason codes (Enter submits, Esc cancels); Ship goes straight through.
//
// Key scope: the viewer keeps `1` for "zoom to 100%" while it has focus (and anything marked `data-plate-keys` keeps
// its own digits), so plain digits are ignored there. Shift+1/2/3 always gives a verdict, even from the viewer.

import { type ReactElement, useCallback, useEffect, useState } from "react";
import { reasonGroups } from "./reasonCodes";
import { isFullScrub, QUADRANT_NAMES, quadrantsVisited } from "./scrub";
import { usePanelsStore, VERDICT_LABEL } from "./store";
import { C, S, tint } from "./theme";
import type { VerdictName } from "./types";

export const VERDICT_KEYS: Readonly<Record<string, VerdictName>> = { "1": "ship", "2": "fix", "3": "render_it" };
const CODE_KEYS: Readonly<Record<string, VerdictName>> = { Digit1: "ship", Digit2: "fix", Digit3: "render_it" };
const NEEDS_REASONS: ReadonlySet<VerdictName> = new Set<VerdictName>(["fix", "render_it"]);
const NON_TEXT_INPUTS = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image"]);

/** True for elements that take typed characters; digits typed there are text, not verdicts. */
export function isTextEntry(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") return !NON_TEXT_INPUTS.has((el as HTMLInputElement).type);
  // jsdom has no isContentEditable, so the attribute is read as well
  const attr = el.getAttribute("contenteditable");
  return (el as HTMLElement).isContentEditable === true || attr === "" || attr === "true" || attr === "plaintext-only";
}

/** Which verdict a key event asks for, or null. `inViewer`: the event came from inside the viewer or a key-owning widget. */
export function verdictForKey(e: Pick<KeyboardEvent, "key" | "code" | "shiftKey">, inViewer: boolean): VerdictName | null {
  if (e.shiftKey) return CODE_KEYS[e.code] ?? null;
  return inViewer ? null : (VERDICT_KEYS[e.key] ?? null);
}

function Quadrants(): ReactElement {
  const scrub = usePanelsStore((s) => s.scrub);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 20px)", gap: 2 }} data-testid="scrub-quadrants" aria-hidden="true">
      {scrub.quadrants.map((seen, i) => (
        <span
          key={QUADRANT_NAMES[i]}
          title={`${QUADRANT_NAMES[i]}: ${seen ? "inspected at 100%" : "not yet"}`}
          data-visited={seen}
          data-quadrant={QUADRANT_NAMES[i]}
          style={{ height: 12, borderRadius: 2, background: seen ? C.pass : C.raised, border: `1px solid ${C.line}` }}
        />
      ))}
    </div>
  );
}

export function VerdictPanel(): ReactElement {
  const runId = usePanelsStore((s) => s.selectedRunId);
  const scrub = usePanelsStore((s) => s.scrub);
  const logged = usePanelsStore((s) => (s.selectedRunId ? s.scrubLogs[s.selectedRunId] : undefined));
  const given = usePanelsStore((s) => (s.selectedRunId ? s.verdicts[s.selectedRunId] : undefined));
  const outcome = usePanelsStore((s) => s.outcome);
  const rater = usePanelsStore((s) => s.rater);
  const setRater = usePanelsStore((s) => s.setRater);
  const logScrub = usePanelsStore((s) => s.logScrub);
  const recordVerdict = usePanelsStore((s) => s.recordVerdict);

  const [pending, setPending] = useState<VerdictName | null>(null);
  const [reasons, setReasons] = useState<string[]>([]);

  // a different candidate starts a fresh verdict
  useEffect(() => {
    setPending(null);
    setReasons([]);
  }, [runId]);

  const submit = useCallback(
    async (verdict: VerdictName, picked: string[]) => {
      const out = await recordVerdict(verdict, picked);
      if (out.ok) {
        setPending(null);
        setReasons([]);
      }
    },
    [recordVerdict],
  );

  const choose = useCallback(
    (verdict: VerdictName) => {
      if (NEEDS_REASONS.has(verdict)) {
        // refuse early so the picker never opens for a verdict that cannot be recorded
        const s = usePanelsStore.getState();
        if (!s.selectedRunId || !s.scrubLogs[s.selectedRunId]) {
          void recordVerdict(verdict, []); // sets the refusal message
          return;
        }
        setPending(verdict);
        setReasons([]);
      } else {
        setPending(null);
        void submit(verdict, []);
      }
    },
    [recordVerdict, submit],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target instanceof Element ? e.target : null;
      if (isTextEntry(target)) return;
      if (pending && e.key === "Escape") {
        setPending(null);
        setReasons([]);
        return;
      }
      if (pending && e.key === "Enter" && !(target instanceof HTMLButtonElement)) {
        e.preventDefault();
        void submit(pending, reasons);
        return;
      }
      const inViewer = target?.closest('[data-testid="viewer"], [data-plate-keys]') != null;
      const verdict = verdictForKey(e, inViewer);
      if (!verdict) return;
      e.preventDefault();
      choose(verdict);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending, reasons, submit, choose]);

  const full = isFullScrub(scrub);
  const canTick = Boolean(runId) && full && !logged;

  return (
    <section style={S.section} aria-label="Verdict" data-testid="verdict-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Verdict</h3>
        <span style={S.muted} data-testid="verdict-run">
          {runId ? `run ${runId.slice(0, 8)}` : "no candidate selected"}
        </span>
      </div>
      <div style={S.row}>
        <Quadrants />
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={S.muted} data-testid="scrub-count">
            {quadrantsVisited(scrub)}/4 quadrants at 100%
          </span>
          <label style={S.row} title={canTick || logged ? "" : "View all four quadrants at 100% or more first"}>
            <input
              type="checkbox"
              checked={Boolean(logged)}
              disabled={!canTick}
              onChange={() => void logScrub()}
              data-testid="full-scrub"
            />
            Full scrub{logged ? " (logged)" : ""}
          </label>
        </div>
      </div>
      <div style={S.row} role="group" aria-label="Verdict keys">
        {(Object.entries(VERDICT_KEYS) as [string, VerdictName][]).map(([key, verdict]) => (
          <button
            key={key}
            type="button"
            style={{ ...S.btn, ...(given === verdict ? { borderColor: C.accent, color: C.accent } : {}) }}
            disabled={!runId}
            data-testid={`verdict-${verdict}`}
            onClick={() => choose(verdict)}
          >
            <kbd>{key}</kbd> {VERDICT_LABEL[verdict]}
          </button>
        ))}
      </div>
      {pending ? (
        <fieldset style={{ border: `1px solid ${C.line}`, borderRadius: 4, margin: 0, padding: 6 }} data-testid="reason-picker">
          <legend style={S.muted}>Why {VERDICT_LABEL[pending]}? (Enter submits, Esc cancels)</legend>
          {reasonGroups().map((g) => (
            <div key={g.group} style={{ marginBottom: 4 }}>
              <div style={S.muted}>{g.title}</div>
              <div style={S.row}>
                {g.codes.map((c) => (
                  <label key={c.code} style={tint(reasons.includes(c.code) ? C.accent : C.muted)} title={c.text}>
                    <input
                      type="checkbox"
                      checked={reasons.includes(c.code)}
                      onChange={(e) =>
                        setReasons((cur) => (e.target.checked ? [...cur, c.code] : cur.filter((x) => x !== c.code)))
                      }
                      aria-label={`${c.code} ${c.text}`}
                      style={{ marginRight: 3 }}
                    />
                    {c.code}
                  </label>
                ))}
              </div>
            </div>
          ))}
          <div style={S.row}>
            <button type="button" style={{ ...S.btn, ...S.btnPrimary }} onClick={() => void submit(pending, reasons)}>
              Submit {VERDICT_LABEL[pending]}
            </button>
            <button
              type="button"
              style={S.btn}
              onClick={() => {
                setPending(null);
                setReasons([]);
              }}
            >
              Cancel
            </button>
          </div>
        </fieldset>
      ) : null}
      <label style={S.row}>
        <span style={S.muted}>Rater</span>
        <input style={{ ...S.input, flex: 1 }} value={rater} onChange={(e) => setRater(e.target.value)} aria-label="Rater" />
      </label>
      {outcome ? (
        <p style={{ margin: 0, color: outcome.ok ? C.pass : C.fail }} role={outcome.ok ? "status" : "alert"} data-testid="verdict-outcome">
          {outcome.message}
        </p>
      ) : null}
    </section>
  );
}

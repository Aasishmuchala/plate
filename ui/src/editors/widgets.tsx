// Small shared widgets for the node editors: the frame every editor sits in, sections, chips, labelled number and
// range fields, a notice line and an async-action hook. Inline styles from ./theme.

import { type ReactElement, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { E, chipStyle, type Tone } from "./theme";

export function EditorFrame({ kind, nodeId, title, children }: { kind: string; nodeId: string; title: string; children: ReactNode }): ReactElement {
  return (
    <section data-testid={`editor-${kind}`} data-node={nodeId} aria-label={`${title} editor`} style={E.frame}>
      <div style={E.head}>
        <h3 style={E.title}>{title}</h3>
        <span style={E.sub}>{nodeId}</span>
      </div>
      {children}
    </section>
  );
}

export function Section({ title, testId, children }: { title: string; testId?: string; children: ReactNode }): ReactElement {
  return (
    <div style={E.section} data-testid={testId}>
      <h4 style={E.h}>{title}</h4>
      {children}
    </div>
  );
}

export function Chip({ tone = "muted", title, testId, children }: { tone?: Tone; title?: string; testId?: string; children: ReactNode }): ReactElement {
  return (
    <span style={chipStyle(tone)} title={title} data-testid={testId} data-tone={tone}>
      {children}
    </span>
  );
}

export function Notice({ tone = "info", testId, children }: { tone?: "info" | "error"; testId?: string; children: ReactNode }): ReactElement {
  return (
    <p role={tone === "error" ? "alert" : "status"} style={tone === "error" ? E.error : E.note} data-testid={testId} data-tone={tone}>
      {children}
    </p>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  on,
  testId,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  on?: boolean;
  testId?: string;
  title?: string;
}): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on === undefined ? undefined : on}
      data-testid={testId}
      title={title}
      style={{ ...E.button, ...(on ? E.buttonOn : null), ...(disabled ? E.buttonOff : null) }}
    >
      {children}
    </button>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <label style={E.field}>
      <span style={E.label}>{label}</span>
      {children}
    </label>
  );
}

/** A number input that keeps what the user is typing and reports only finite values inside [min, max]. */
export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  testId,
  suffix,
}: {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  step?: number | "any";
  testId?: string;
  suffix?: string;
}): ReactElement {
  const [text, setText] = useState(value === null ? "" : String(value));
  // `emitted` is the last value this field reported; `seen` the prop it last rendered with. The text is rewritten only
  // when the prop changes to something the field did not itself report, so clearing the box to retype stays cleared.
  const emitted = useRef(value);
  const seen = useRef(value);
  useEffect(() => {
    if (value === seen.current) return;
    seen.current = value;
    if (value !== emitted.current) {
      emitted.current = value;
      setText(value === null ? "" : String(value));
    }
  }, [value]);
  return (
    <Field label={label}>
      <input
        type="number"
        style={E.number}
        value={text}
        min={min}
        max={max}
        step={step}
        data-testid={testId}
        onChange={(e) => {
          setText(e.target.value);
          if (e.target.value.trim() === "") {
            emitted.current = null;
            onChange(null);
            return;
          }
          const v = Number(e.target.value);
          if (!Number.isFinite(v)) return;
          if ((min !== undefined && v < min) || (max !== undefined && v > max)) return;
          emitted.current = v;
          onChange(v);
        }}
      />
      {suffix ? <span style={E.note}>{suffix}</span> : null}
    </Field>
  );
}

export function RangeField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  testId,
  format,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  testId?: string;
  format?: (v: number) => string;
}): ReactElement {
  return (
    <Field label={label}>
      <input
        type="range"
        style={{ flex: 1, minWidth: 0 }}
        value={value}
        min={min}
        max={max}
        step={step}
        data-testid={testId}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <output style={{ ...E.note, minWidth: 44, textAlign: "right" }} data-testid={testId ? `${testId}-value` : undefined}>
        {format ? format(value) : String(value)}
      </output>
    </Field>
  );
}

export function Toggle({ label, checked, onChange, testId, title }: { label: string; checked: boolean; onChange: (v: boolean) => void; testId?: string; title?: string }): ReactElement {
  return (
    <label style={E.field} title={title}>
      <input type="checkbox" checked={checked} data-testid={testId} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

export interface ActionState {
  pending: boolean;
  error: string | null;
  clear: () => void;
}

/**
 * Wrap an async editor action: `run(...)` flags pending, keeps the last error text and ignores a result that arrives
 * after the editor unmounted. The wrapped function's own return value is passed through.
 */
export function useAction<A extends unknown[], R>(
  fn: (...args: A) => Promise<R>,
  describe: (err: unknown) => string,
): [(...args: A) => Promise<R | undefined>, ActionState] {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setPending(true);
      setError(null);
      try {
        return await fnRef.current(...args);
      } catch (err) {
        if (alive.current) setError(describe(err));
        return undefined;
      } finally {
        if (alive.current) setPending(false);
      }
    },
    [describe],
  );
  const clear = useCallback(() => setError(null), []);
  return [run, { pending, error, clear }];
}

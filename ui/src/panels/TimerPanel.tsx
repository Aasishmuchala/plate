// Human-minute timer. One deliverable at a time: Space (with this panel focused) or the button starts and stops the
// timer, and the daemon ledgers the elapsed human minutes. "Today" lists the minutes per deliverable the daemon has
// recorded. The bet is measured in human minutes per deliverable, so this is a measurement tool, not decoration.

import { type KeyboardEvent, type ReactElement, useEffect, useState } from "react";
import { errorMessage, getTimerToday, timerAction } from "./api";
import { formatElapsed, formatMinutes } from "./format";
import { C, S } from "./theme";
import { useLoad } from "./useLoad";

export const DELIVERABLE_KEY = "plate.timer.deliverable";

function readDeliverable(): string {
  try {
    return window.localStorage.getItem(DELIVERABLE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function TimerPanel(): ReactElement {
  const [deliverable, setDeliverable] = useState(readDeliverable);
  const [running, setRunning] = useState<{ id: string; since: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const today = useLoad("timer-today", getTimerToday);

  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);

  const toggle = async () => {
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      if (running) {
        await timerAction(running.id, "stop");
        setRunning(null);
        today.reload();
      } else {
        const id = deliverable.trim();
        if (!id) {
          setError("Name the deliverable first.");
          return;
        }
        await timerAction(id, "start");
        try {
          window.localStorage.setItem(DELIVERABLE_KEY, id);
        } catch {
          // private mode: the name lasts for this tab only
        }
        const t = Date.now();
        setNow(t);
        setRunning({ id, since: t });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // only when the panel itself has focus: a focused button or text box keeps its own Space
    if (e.target === e.currentTarget && e.key === " ") {
      e.preventDefault();
      void toggle();
    }
  };

  const rows = today.data ?? [];
  return (
    <section style={S.section} aria-label="Timer" data-testid="timer-panel" tabIndex={0} onKeyDown={onKeyDown}>
      <h3 style={S.h}>Timer</h3>
      <div style={S.row}>
        <input
          style={{ ...S.input, flex: 1 }}
          value={running ? running.id : deliverable}
          onChange={(e) => setDeliverable(e.target.value)}
          disabled={running !== null}
          placeholder="deliverable id"
          aria-label="Deliverable id"
          list="timer-deliverables"
        />
        <datalist id="timer-deliverables">
          {rows.map((r) => (
            <option key={r.deliverable_id} value={r.deliverable_id} />
          ))}
        </datalist>
        <button type="button" style={{ ...S.btn, ...(running ? {} : S.btnPrimary) }} disabled={busy} onClick={() => void toggle()}>
          {running ? "Stop" : "Start"}
        </button>
      </div>
      <p style={{ margin: 0, color: running ? C.pass : C.muted }} data-testid="timer-state" role="status">
        {running ? `Running ${formatElapsed(now - running.since)}` : "Stopped. Focus this panel and press Space to start."}
      </p>
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      <h3 style={S.h}>Today</h3>
      {today.error ? (
        <p style={S.error} role="alert">
          {today.error}
        </p>
      ) : null}
      {!today.loading && !today.error && rows.length === 0 ? <p style={S.muted}>No minutes recorded today.</p> : null}
      <ul style={S.list} data-testid="timer-today">
        {rows.map((r) => (
          <li key={r.deliverable_id} style={{ ...S.row, justifyContent: "space-between" }} data-testid="timer-row">
            <span style={S.mono}>{r.deliverable_id}</span>
            <span>{formatMinutes(r.minutes)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

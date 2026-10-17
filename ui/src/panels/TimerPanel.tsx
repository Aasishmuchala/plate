// Human-minute timer. One deliverable at a time: Space (with this panel focused) or the button starts and stops the
// timer, and the daemon ledgers the elapsed human minutes. "Today" lists the minutes per deliverable the daemon has
// recorded. The bet is measured in human minutes per deliverable, so this is a measurement tool, not decoration.
// The daemon owns the timer: `GET /timer/today` says which one is running (a reload, or another window, started it),
// and a Start refused with `409 timer_running` names the deliverable to stop first.

import { type KeyboardEvent, type ReactElement, useEffect, useState } from "react";
import { errorMessage, getTimerToday, timerAction, timerConflict, timerNotRunning } from "./api";
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
  const [conflict, setConflict] = useState<string | null>(null);
  const today = useLoad("timer-today", getTimerToday);

  // A timer the daemon reports as running is this panel's running timer (on mount, and after each start or stop).
  useEffect(() => {
    const r = today.data?.running;
    if (!r) return;
    setRunning({ id: r.deliverable_id, since: r.since });
    setNow(Date.now());
    setConflict(null);
  }, [today.data]);

  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);

  const toggle = async () => {
    if (busy) return;
    setError(null);
    setConflict(null);
    setBusy(true);
    try {
      if (running) {
        try {
          await timerAction(running.id, "stop");
        } catch (err) {
          if (timerNotRunning(err)) {
            setRunning(null); // the daemon has no timer: whatever this panel believed is over
            today.reload();
          }
          throw err;
        }
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
        today.reload();
      }
    } catch (err) {
      const other = running ? null : timerConflict(err);
      if (other) setConflict(other);
      else setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  // The refusal's one-click way out: stop the timer that is in the way, then the artist starts theirs.
  const stopConflicting = async () => {
    if (busy || !conflict) return;
    setError(null);
    setBusy(true);
    try {
      await timerAction(conflict, "stop");
      setConflict(null);
      today.reload();
    } catch (err) {
      if (timerNotRunning(err)) {
        setConflict(null);
        today.reload();
      }
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

  const rows = today.data?.rows ?? [];
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
        {running ? `Running ${formatElapsed(Math.max(0, now - running.since))}` : "Stopped. Focus this panel and press Space to start."}
      </p>
      {conflict ? (
        <p style={{ ...S.error, ...S.row }} role="alert" data-testid="timer-conflict">
          <span>{`stop ${conflict} first`}</span>
          <button type="button" style={S.btn} disabled={busy} onClick={() => void stopConflicting()} data-testid="timer-conflict-stop">
            Stop
          </button>
        </p>
      ) : null}
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

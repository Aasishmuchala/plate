// Status-bar hook: polls `/api/v0/health` (no token needed), reports the token state, and keeps the last bus
// `alert` (T71: unattended-spend and nightly-job alerts the daemon publishes) until it is dismissed.
// T147: the token state is honest (`present`, `rejected` once any call answered 401, `none`: api/client), and the
// daemon coming back (`down` -> `up`) sends a pending graph save at once instead of waiting for the next retry.

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { create } from "zustand";
import { hub } from "../canvas/runEvents";
import type { BusRecord } from "../canvas/runTypes";
import { saveIfPending } from "../store";
import { getHealth, type Health, onTokenChange, tokenGeneration, tokenState, type TokenState } from "./client";

export type DaemonState = "checking" | "up" | "down";

export type AlertLevel = "info" | "warning" | "error";

/** The newest `alert` record from the event bus; `at` is the record's time (ISO-8601 UTC). */
export interface DaemonAlert {
  level: AlertLevel;
  message: string;
  at: string;
}

export interface DaemonStatus {
  state: DaemonState;
  health: Health | null;
  /** This tab holds a token the daemon has not refused. */
  tokenPresent: boolean;
  tokenState: TokenState;
  lastAlert: DaemonAlert | null;
  dismissAlert: () => void;
}

/** `warning` / `error` as the daemon spells them (and the usual synonyms); anything else is plain information. */
export function alertLevel(raw: unknown): AlertLevel {
  const v = typeof raw === "string" ? raw.toLowerCase() : "";
  if (v === "error" || v === "critical" || v === "fatal") return "error";
  if (v === "warning" || v === "warn") return "warning";
  return "info";
}

/** The alert inside an `alert` bus record (`data.level`, `data.message`), or null when it carries no message. */
export function alertFromRecord(rec: BusRecord): DaemonAlert | null {
  const data = rec.data ?? {};
  const message = typeof data["message"] === "string" ? data["message"].trim() : "";
  if (message === "") return null;
  const at = typeof rec.at === "string" && rec.at !== "" ? rec.at : new Date().toISOString();
  return { level: alertLevel(data["level"]), message, at };
}

interface AlertState {
  lastAlert: DaemonAlert | null;
  show: (alert: DaemonAlert) => void;
  dismiss: () => void;
}

/** One alert for the whole tab: every component that reads it sees the same one. */
export const useAlertStore = create<AlertState>()((set) => ({
  lastAlert: null,
  show: (alert) => set({ lastAlert: alert }),
  dismiss: () => set({ lastAlert: null }),
}));

/** Subscribes to `alert` records while mounted; returns the newest one and its dismiss. */
export function useLastAlert(): { lastAlert: DaemonAlert | null; dismissAlert: () => void } {
  const lastAlert = useAlertStore((s) => s.lastAlert);
  const dismissAlert = useAlertStore((s) => s.dismiss);
  useEffect(
    () =>
      hub.onKind("alert", (rec) => {
        const alert = alertFromRecord(rec);
        if (alert) useAlertStore.getState().show(alert);
      }),
    [],
  );
  return { lastAlert, dismissAlert };
}

/** The token state, re-read whenever a call is refused or a token is set. */
export function useTokenState(): TokenState {
  return useSyncExternalStore(onTokenChange, () => tokenState());
}

/** How many tokens were set in this tab (bumped by a paste), for re-reading everything with the new one. */
export function useTokenGeneration(): number {
  return useSyncExternalStore(onTokenChange, tokenGeneration);
}

export function useDaemonStatus(pollMs = 15000): DaemonStatus {
  const [status, setStatus] = useState<Pick<DaemonStatus, "state" | "health">>({ state: "checking", health: null });
  const token = useTokenState();
  const { lastAlert, dismissAlert } = useLastAlert();
  const seen = useRef<DaemonState>("checking");

  useEffect(() => {
    let alive = true;
    const check = () => {
      getHealth().then(
        (health) => {
          if (alive) setStatus({ state: health.ok ? "up" : "down", health });
        },
        () => {
          if (alive) setStatus({ state: "down", health: null });
        },
      );
    };
    check();
    const timer = window.setInterval(check, pollMs);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [pollMs]);

  // The daemon is back: send what is waiting rather than wait for the next retry.
  useEffect(() => {
    const was = seen.current;
    seen.current = status.state;
    if (was === "down" && status.state === "up") saveIfPending();
  }, [status.state]);

  return { ...status, tokenPresent: token === "present", tokenState: token, lastAlert, dismissAlert };
}

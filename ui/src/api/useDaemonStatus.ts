// Status-bar hook: polls `/api/v0/health` (no token needed), reports whether this tab holds a token, and keeps the
// last bus `alert` (T71: unattended-spend and nightly-job alerts the daemon publishes) until it is dismissed.

import { useEffect, useState } from "react";
import { create } from "zustand";
import { hub } from "../canvas/runEvents";
import type { BusRecord } from "../canvas/runTypes";
import { getHealth, getToken, type Health } from "./client";

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
  tokenPresent: boolean;
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

export function useDaemonStatus(pollMs = 15000): DaemonStatus {
  const [status, setStatus] = useState<Pick<DaemonStatus, "state" | "health" | "tokenPresent">>({
    state: "checking",
    health: null,
    tokenPresent: false,
  });
  const { lastAlert, dismissAlert } = useLastAlert();

  useEffect(() => {
    let alive = true;
    const check = () => {
      getHealth().then(
        (health) => {
          if (alive) setStatus({ state: health.ok ? "up" : "down", health, tokenPresent: getToken() !== null });
        },
        () => {
          if (alive) setStatus({ state: "down", health: null, tokenPresent: getToken() !== null });
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

  return { ...status, lastAlert, dismissAlert };
}

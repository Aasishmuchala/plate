// Status-bar hook: polls `/api/v0/health` (no token needed) and reports whether this tab holds a token.

import { useEffect, useState } from "react";
import { getHealth, getToken, type Health } from "./client";

export type DaemonState = "checking" | "up" | "down";

export interface DaemonStatus {
  state: DaemonState;
  health: Health | null;
  tokenPresent: boolean;
}

export function useDaemonStatus(pollMs = 15000): DaemonStatus {
  const [status, setStatus] = useState<DaemonStatus>({ state: "checking", health: null, tokenPresent: false });

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

  return status;
}

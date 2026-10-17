// Run routes the canvas calls after a Make (T25 contract): read a run, read its gate report, override a GATE_FAIL.

import { fetchJson, postJson } from "../api/client";
import type { GateReportWire, RunWire } from "./runTypes";

/** Who the canvas acts as. The daemon has one bearer token per install, so a browser action is a human's. */
export const UI_ACTOR = "human";

const enc = encodeURIComponent;

export const getRun = (runId: string): Promise<RunWire> => fetchJson<RunWire>(`/runs/${enc(runId)}`);

export const getGateReport = (runId: string): Promise<GateReportWire> => fetchJson<GateReportWire>(`/runs/${enc(runId)}/gate`);

/** `POST /runs/{id}/override {reason, by}`: a GATE_FAIL becomes exportable, with the reason in the ledger. */
export function overrideRun(runId: string, reason: string, by: string = UI_ACTOR): Promise<RunWire | null> {
  const text = reason.trim();
  if (text === "") return Promise.reject(new Error("an override needs a reason"));
  return postJson<RunWire | null>(`/runs/${enc(runId)}/override`, { reason: text, by });
}

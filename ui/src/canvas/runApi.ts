// Run routes the canvas calls after a Make (T25 contract): read a run, read its gate report, override a GATE_FAIL,
// accept a take after its logged full scrub (doctrine 7), cancel a run that has not finished.

import { fetchJson, postJson } from "../api/client";
import type { GateReportWire, RunWire } from "./runTypes";

/** Who the canvas acts as. The daemon has one bearer token per install, so a browser action is a human's. */
export const UI_ACTOR = "human";

const enc = encodeURIComponent;

export const getRun = (runId: string): Promise<RunWire> => fetchJson<RunWire>(`/runs/${enc(runId)}`);

/** The latest gate report: the report itself, or `{report, n, shipped, versions}` (both shapes are accepted). */
export async function getGateReport(runId: string): Promise<GateReportWire> {
  const body = await fetchJson<unknown>(`/runs/${enc(runId)}/gate`);
  if (body && typeof body === "object" && "report" in body) return (body as { report: GateReportWire }).report;
  return body as GateReportWire;
}

/** `POST /runs/{id}/override {reason, by}`: a GATE_FAIL becomes exportable, with the reason in the ledger. */
export function overrideRun(runId: string, reason: string, by: string = UI_ACTOR): Promise<RunWire | null> {
  const text = reason.trim();
  if (text === "") return Promise.reject(new Error("an override needs a reason"));
  return postJson<RunWire | null>(`/runs/${enc(runId)}/override`, { reason: text, by });
}

/** `POST /runs/{id}/accept {scrub_log_id, by}`: the daemon accepts a DONE (or overridden) take only with the id of a
 * logged full scrub of that run. */
export function acceptRun(runId: string, scrubLogId: string, by: string = UI_ACTOR): Promise<RunWire> {
  if (scrubLogId.trim() === "") return Promise.reject(new Error("accepting a take needs a logged full scrub"));
  return postJson<RunWire>(`/runs/${enc(runId)}/accept`, { scrub_log_id: scrubLogId, by });
}

/** `POST /runs/{id}/cancel {reason?}`: stop a run that is queued or out with an engine; the daemon logs the reason. */
export function cancelRun(runId: string, reason?: string): Promise<RunWire> {
  const text = reason?.trim();
  return postJson<RunWire>(`/runs/${enc(runId)}/cancel`, text ? { reason: text } : {});
}

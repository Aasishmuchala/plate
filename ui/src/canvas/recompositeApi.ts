// Recomposite route (T99, decision 54): `POST /runs/{id}/recomposite {feather_px?, grow_px?, mask_hash?}` puts a stored
// raw take through the Edit node's Mask as it stands now (or `mask_hash`, a plate-sized grey PNG already in the project's
// CAS), with a feather inside the mask and an optional grow or shrink. The daemon quotes it at 0 USD, approves it as the
// human who pressed the button, runs it (no engine is called) and answers the new run as `GET /runs/{id}` does.

import { postJson } from "../api/client";
import type { RunWire } from "./runTypes";

/** Feather inside the mask, px (decision 4). */
export const FEATHER_MIN = 4;
export const FEATHER_MAX = 12;
/** Dilate (+) or erode (-) the editable mask, px; the daemon's elliptical kernel is 2 * |px| + 1 wide. */
export const GROW_MIN = -32;
export const GROW_MAX = 32;

export interface RecompositeRequest {
  /** Left out: the take's own feather. */
  feather_px?: number;
  grow_px?: number;
  /** A 64-hex CAS key of a plate-sized grey PNG that replaces the Mask node's mask. */
  mask_hash?: string;
}

const HEX64 = /^[0-9a-f]{64}$/;

/** Why a request would be refused, or null when it is fine (the daemon checks the same limits). */
export function checkRecomposite(req: RecompositeRequest): string | null {
  const { feather_px: feather, grow_px: grow, mask_hash: hash } = req;
  if (feather !== undefined && !(Number.isFinite(feather) && feather >= FEATHER_MIN && feather <= FEATHER_MAX)) {
    return `feather must be from ${FEATHER_MIN} to ${FEATHER_MAX} px`;
  }
  if (grow !== undefined && !(Number.isInteger(grow) && grow >= GROW_MIN && grow <= GROW_MAX)) {
    return `grow must be a whole number of px from ${GROW_MIN} to ${GROW_MAX}`;
  }
  if (hash !== undefined && !HEX64.test(hash)) return "mask_hash must be a CAS key (64 hex)";
  return null;
}

/** The new run: DONE (or GATE_FAIL) already, at 0 USD, on the `local-recomposite` route. */
export function recomposite(runId: string, req: RecompositeRequest = {}): Promise<RunWire> {
  const problem = checkRecomposite(req);
  if (problem) return Promise.reject(new Error(problem));
  return postJson<RunWire>(`/runs/${encodeURIComponent(runId)}/recomposite`, req);
}

// Calls behind the Project panel's "Import client plate" form and its custody row (T72). The routes are T38's
// `POST /projects/{id}/tierc` and `POST /projects/{id}/kit` and T59's `PUT /projects/{id}/custody`. Both imports
// answer 202 with the packet the daemon is ingesting; the packet shows up in the Plate picker when its
// `packet.ingested` event arrives, so nothing here waits for it.

import { postJson, putJson } from "../api";
import type { CustodyGrade, ProjectCustody } from "./types";

const enc = encodeURIComponent;

/** The daemon's 428 text for a Tier C plate without a consent record (decision 10); the form refuses with the same words. */
export const CONSENT_REQUIRED = "consent record required";
/** `TierCBody.consent_record` is capped at this many characters by the route. */
export const MAX_CONSENT_CHARS = 2000;
/** A loosened custody setting must carry a reason of at least this many characters (T59). */
export const MIN_REASON_CHARS = 10;

export interface TierCImport {
  plate_path: string;
  shot_id: string;
  consent_record: string;
}

export interface KitImport {
  folder: string;
  camera: string;
  plate_path: string;
  /** Optional: the daemon derives one from the camera when absent. */
  shot_id?: string;
}

/** The 202 body of both import routes (`register_packet_folder` plus the route's extras). */
export interface ImportAccepted {
  packet_id: string;
  /** `ingesting` normally; `ingested` when the daemon ingests synchronously. */
  status: string;
  tier?: string;
  packet_dir?: string;
  events_url?: string;
  /** Tier C: true when the pixels worker ran its stub models. */
  stub?: boolean;
  /** Kit: what the converter flagged or warned about. */
  flags?: string[];
  warnings?: string[];
}

export interface CustodyEdit {
  nda: boolean;
  allowed_grades: CustodyGrade[];
  /** Required by the daemon when the edit loosens the setting. */
  reason?: string;
}

export const importTierC = (projectId: string, body: TierCImport): Promise<ImportAccepted> =>
  postJson<ImportAccepted>(`/projects/${enc(projectId)}/tierc`, body);

export const importKit = (projectId: string, body: KitImport): Promise<ImportAccepted> =>
  postJson<ImportAccepted>(`/projects/${enc(projectId)}/kit`, body);

export const putCustody = (projectId: string, body: CustodyEdit): Promise<unknown> =>
  putJson(`/projects/${enc(projectId)}/custody`, body);

/**
 * Whether moving from `before` to `after` loosens the project's custody (T59): an NDA project turned non-NDA, or,
 * with the NDA flag unchanged, a grade set that is not a subset of the old one. Turning NDA on is always tightening.
 * The daemon decides in the end; this only says whether to ask for a reason before posting.
 */
export function loosensCustody(before: ProjectCustody, after: Pick<ProjectCustody, "nda" | "allowed_grades">): boolean {
  if (after.nda && !before.nda) return false;
  if (before.nda && !after.nda) return true;
  return after.allowed_grades.some((g) => !before.allowed_grades.includes(g));
}

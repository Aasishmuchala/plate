// Light node editor (T47): match the plate's lighting to a reference image in Max. "Match in Max" asks the daemon to
// queue a `light.match` job; the Max dock picks it up, runs MaxGaffer on the camera with the locks, re-renders and
// sends a packet that supersedes this one. "Vantage preview" queues `light.vantage_preview` (the live link). Locked
// parameters are never touched by the match. After queuing, the editor polls `GET /plugin/jobs/{id}` every 3 s and
// follows the job queued -> claimed -> done | failed, showing the dock's `detail` and the packet it sent.
// Node params: `referenceHash` (CAS hash of the reference image), `camera` (Max camera name; empty = the packet's),
// `locks` (MaxGaffer parameter keys), `deep` (deep match).

import { type ReactElement, useEffect, useState } from "react";
import { ApiError } from "../api";
import { useThumb } from "../canvas/assets";
import { errorMessage, getPluginJob, PLUGIN_JOB_FINAL, type PluginJob, queuePluginJob } from "./api";
import { asBool, asString, asStringList, useNodeEditor, usePlate, useStill } from "./params";
import { E, type Tone } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, Toggle, useAction } from "./widgets";

/** MaxGaffer's fixed rig parameters (`core/genome.py` PARAMS), grouped for the lock menu. */
export const LOCK_GROUPS: readonly { group: string; keys: readonly string[] }[] = [
  { group: "Sun", keys: ["sun.enabled", "sun.azimuth_deg", "sun.altitude_deg", "sun.intensity", "sun.size", "sun.turbidity"] },
  { group: "Dome", keys: ["dome.enabled", "dome.rotation_deg", "dome.intensity"] },
  { group: "Exposure", keys: ["exposure.ev", "exposure.wb_kelvin"] },
];

/** How often a queued job is asked about (ms). */
export const JOB_POLL_MS = 3000;

const JOB_VERB: Readonly<Record<string, string>> = {
  queued: "queued",
  claimed: "claimed by Max",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
};
const JOB_TONE: Readonly<Record<string, Tone>> = { queued: "info", claimed: "info", done: "pass", failed: "fail", cancelled: "advisory" };

/** The three stops of a job: queued, claimed, then where it ended (done until it fails or is cancelled). */
export function jobSteps(state: string): [string, string, string] {
  return ["queued", "claimed", state === "failed" || state === "cancelled" ? state : "done"];
}

export function LightEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const upstream = useStill(nodeId);
  const [job, setJob] = useState<{ kind: string; job: PluginJob } | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [lost, setLost] = useState(false);

  const referenceHash = asString(params.referenceHash);
  const camera = asString(params.camera);
  const locks = asStringList(params.locks);
  const deep = asBool(params.deep);
  const packetId = plate?.packetId ?? null;
  const thumb = useThumb(referenceHash || null);

  const toggleLock = (key: string, on: boolean): void => {
    const next = on ? Array.from(new Set([...locks, key])) : locks.filter((k) => k !== key);
    set({ locks: next.length > 0 ? next.sort() : undefined });
  };

  const follow = (kind: string, queued: PluginJob): void => {
    setPollError(null);
    setLost(false);
    setJob({ kind, job: queued });
  };
  const [match, matchState] = useAction(async () => {
    follow("match", await queuePluginJob({ action: "light.match", packet_id: packetId, camera: camera || undefined, reference_hash: referenceHash, locks, deep }));
  }, errorMessage);
  const [vantage, vantageState] = useAction(async () => {
    follow("vantage preview", await queuePluginJob({ action: "light.vantage_preview", packet_id: packetId, camera: camera || undefined }));
  }, errorMessage);

  // follow the job the dock is working on until it is done, failed or cancelled; stop when the editor closes
  const jobId = job?.job.id ?? null;
  const watching = job !== null && !PLUGIN_JOB_FINAL.has(job.job.state) && !lost;
  useEffect(() => {
    if (!watching || jobId === null) return;
    let alive = true;
    let busy = false;
    const timer = setInterval(() => {
      if (busy) return;
      busy = true;
      getPluginJob(jobId)
        .then((next) => {
          if (!alive) return;
          setPollError(null);
          setJob((cur) => (cur && cur.job.id === jobId ? { ...cur, job: next } : cur));
        })
        .catch((err: unknown) => {
          if (!alive) return;
          if (err instanceof ApiError && err.status === 404) setLost(true);
          setPollError(errorMessage(err));
        })
        .finally(() => {
          busy = false;
        });
    }, JOB_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [watching, jobId]);

  const error = matchState.error ?? vantageState.error;

  return (
    <EditorFrame kind="light" nodeId={nodeId} title="Light">
      <Section title="Reference" testId="light-reference">
        <Field label="image">
          <input
            type="text"
            style={E.wide}
            value={referenceHash}
            placeholder="CAS hash of the reference image"
            data-testid="light-reference-hash"
            onChange={(e) => set({ referenceHash: e.target.value.trim() || undefined })}
          />
        </Field>
        <div style={E.row}>
          <Button onClick={() => upstream?.hash && set({ referenceHash: upstream.hash })} disabled={!upstream?.hash} testId="light-use-upstream" title="use the nearest upstream image as the reference">
            Use upstream image
          </Button>
          <Field label="camera">
            <input type="text" style={E.wide} value={camera} placeholder="Max camera (default: the packet's)" data-testid="light-camera" onChange={(e) => set({ camera: e.target.value || undefined })} />
          </Field>
        </div>
        {thumb.state === "ready" ? (
          <img src={thumb.url} alt="reference" data-testid="light-reference-thumb" style={{ width: "100%", borderRadius: 4 }} />
        ) : referenceHash ? (
          <span style={E.note}>{thumb.state === "loading" ? "loading the reference" : "the reference image is unavailable"}</span>
        ) : null}
      </Section>

      <Section title="Locks (never touched by the match)" testId="light-locks">
        {LOCK_GROUPS.map((g) => (
          <div key={g.group} style={E.row}>
            <span style={{ ...E.label, minWidth: 60 }}>{g.group}</span>
            {g.keys.map((k) => (
              <Toggle key={k} label={k.split(".")[1] ?? k} checked={locks.includes(k)} onChange={(v) => toggleLock(k, v)} testId={`light-lock-${k}`} title={k} />
            ))}
          </div>
        ))}
        <Toggle label="deep match (slower, more iterations)" checked={deep} onChange={(v) => set({ deep: v || undefined })} testId="light-deep" />
      </Section>

      <div style={E.row} data-testid="light-actions">
        <Button onClick={() => void match()} disabled={!packetId || referenceHash === "" || matchState.pending} testId="light-match" title={referenceHash === "" ? "pick a reference image first" : "queue the match for the Max dock"}>
          {matchState.pending ? "Queuing" : "Match in Max"}
        </Button>
        <Button onClick={() => void vantage()} disabled={!packetId || vantageState.pending} testId="light-vantage">
          {vantageState.pending ? "Queuing" : "Vantage preview"}
        </Button>
      </div>
      {job ? (
        <div style={E.row} data-testid="light-job-row">
          <Chip tone={JOB_TONE[job.job.state] ?? "info"} testId="light-job">
            {job.kind} {JOB_VERB[job.job.state] ?? job.job.state}: job {job.job.id} ({job.job.state})
          </Chip>
          <span style={E.note} data-testid="light-job-steps">
            {jobSteps(job.job.state).map((step, i) => (
              <span key={step} data-active={step === job.job.state ? "true" : undefined} style={step === job.job.state ? { fontWeight: 600 } : undefined}>
                {i > 0 ? " \u2192 " : ""}
                {step}
              </span>
            ))}
          </span>
          {job.job.detail ? (
            <span style={E.note} data-testid="light-job-detail">
              {job.job.detail}
            </span>
          ) : null}
          {job.job.resultPacketId ? (
            <Chip tone="pass" testId="light-job-packet">
              packet {job.job.resultPacketId}
            </Chip>
          ) : null}
          {job.kind === "match" && !PLUGIN_JOB_FINAL.has(job.job.state) ? (
            <span style={E.note}>Max re-renders the camera and sends a packet that supersedes this one</span>
          ) : null}
        </div>
      ) : null}
      {pollError ? (
        <Notice tone="error" testId="light-job-error">
          {lost ? `the daemon no longer has this job: ${pollError}` : `could not read the job's state (will retry): ${pollError}`}
        </Notice>
      ) : null}
      {packetId === null ? (
        <Notice testId="light-nopacket">connect this node to a plate with a packet first</Notice>
      ) : null}
      {error ? (
        <Notice tone="error" testId="light-error">
          {error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}

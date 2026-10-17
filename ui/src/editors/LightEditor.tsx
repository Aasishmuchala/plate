// Light node editor (T47): match the plate's lighting to a reference image in Max. "Match in Max" asks the daemon to
// queue a `light.match` job; the Max dock picks it up, runs MaxGaffer on the camera with the locks, re-renders and
// sends a packet that supersedes this one. "Vantage preview" queues `light.vantage_preview` (the live link). Locked
// parameters are never touched by the match.
// Node params: `referenceHash` (CAS hash of the reference image), `camera` (Max camera name; empty = the packet's),
// `locks` (MaxGaffer parameter keys), `deep` (deep match).

import { type ReactElement, useState } from "react";
import { useThumb } from "../canvas/assets";
import { errorMessage, type PluginJob, queuePluginJob } from "./api";
import { asBool, asString, asStringList, useNodeEditor, usePlate, useStill } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, Toggle, useAction } from "./widgets";

/** MaxGaffer's fixed rig parameters (`core/genome.py` PARAMS), grouped for the lock menu. */
export const LOCK_GROUPS: readonly { group: string; keys: readonly string[] }[] = [
  { group: "Sun", keys: ["sun.enabled", "sun.azimuth_deg", "sun.altitude_deg", "sun.intensity", "sun.size", "sun.turbidity"] },
  { group: "Dome", keys: ["dome.enabled", "dome.rotation_deg", "dome.intensity"] },
  { group: "Exposure", keys: ["exposure.ev", "exposure.wb_kelvin"] },
];

export function LightEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const upstream = useStill(nodeId);
  const [job, setJob] = useState<{ kind: string; job: PluginJob } | null>(null);

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

  const [match, matchState] = useAction(async () => {
    setJob({ kind: "match", job: await queuePluginJob({ action: "light.match", packet_id: packetId, camera: camera || undefined, reference_hash: referenceHash, locks, deep }) });
  }, errorMessage);
  const [vantage, vantageState] = useAction(async () => {
    setJob({ kind: "vantage preview", job: await queuePluginJob({ action: "light.vantage_preview", packet_id: packetId, camera: camera || undefined }) });
  }, errorMessage);

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
        <div style={E.row}>
          <Chip tone="info" testId="light-job">
            {job.kind} queued: job {job.job.id} ({job.job.state})
          </Chip>
          {job.kind === "match" ? <span style={E.note}>Max re-renders the camera and sends a packet that supersedes this one</span> : null}
        </div>
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

// Plate node editor (T69): link the node to one of the project's packets. The list comes from
// `GET /projects/{pid}/packets` (shot id, tier, camera, received time, a thumbnail of the display plate, superseded
// packets greyed); a search box filters it. Choosing a packet sets the node's `packetId`, `tier`, `outputHash` (the
// plate's BLAKE3, served by reference) and `outputSize` through the graph store's `setPlatePacket` (which autosaves),
// so every editor downstream works on that camera. The list refreshes on the event hub's `packet.ingested` and
// `packet.superseded` records (`hub.onKind`; App keeps the project's stream open) and on the Refresh button.
// The node has no params of its own: the link lives on the node data the canvas rules read.
//
// A linked packet whose summary has `supersedes` (a client revision of the same camera) also gets a "Carry over
// approved layers from <old id>" button (T96): `POST /packets/{id}/carry-over {from_packet_id}` runs
// `revisions.carry_over` for every layer the artist accepted on the old plate and answers one verdict per layer.
// The list below the button reads `carried · <why>` or `dropped · <why>`; a carried layer is already a new take on
// the new plate that still needs the artist's accept. The packet list is read again after the call.

import { type ReactElement, useEffect, useMemo, useState } from "react";
import { postJson } from "../api";
import { useThumb } from "../canvas/assets";
import { hub, type RecordHandler } from "../canvas/runEvents";
import { asList } from "../panels/api";
import { formatTime } from "../panels/format";
import { type PlatePacketLink, useGraphStore } from "../store";
import { errorMessage } from "./api";
import { listPackets, type PacketItem, type PacketSummary, packetSummary } from "./nodeApi";
import { useNodeEditor } from "./params";
import { C, E, TONE_COLOR } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

/** Records that change the list: a new packet, or one that a re-render superseded. */
export const PACKET_EVENT_KINDS = ["packet.ingested", "packet.superseded"] as const;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const text = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/** One layer's verdict from `POST /packets/{id}/carry-over` (`revisions.carry_over`). */
export interface CarryResult {
  /** The accepted run on the old packet the layer came from. */
  runId: string | null;
  ids: string[];
  status: "carried" | "dropped";
  /** Machine code: `carried`, `already_carried`, `material_changed`, `mask_moved`, ... */
  code: string | null;
  reason: string;
  /** The take that now exists on the new packet (carried layers only). */
  newRunId: string | null;
}

/** One result row; anything the daemon does not call `carried` reads as dropped. Null for a row that is no object. */
export function parseCarryResult(raw: unknown): CarryResult | null {
  if (!isRecord(raw)) return null;
  const status = raw.status === "carried" ? "carried" : "dropped";
  return {
    runId: text(raw.run_id),
    ids: Array.isArray(raw.ids) ? raw.ids.filter((i): i is string => typeof i === "string") : [],
    status,
    code: text(raw.code),
    reason: text(raw.reason) ?? text(raw.detail) ?? (status === "carried" ? "carried" : "no reason given"),
    newRunId: text(raw.new_run_id),
  };
}

/** The route answers a list; `{results: [...]}` (the `packet.carried` record's shape) reads the same. */
export function parseCarryResults(body: unknown): CarryResult[] {
  return asList<unknown>(body, "results").flatMap((r) => parseCarryResult(r) ?? []);
}

/** `POST /packets/{packetId}/carry-over`: carry the layers accepted on `fromPacketId` (default: the manifest's
 * `supersedes`) onto `packetId`. Idempotent: a layer carried before answers `already_carried`. */
export async function carryOver(packetId: string, fromPacketId?: string): Promise<CarryResult[]> {
  const body = await postJson<unknown>(`/packets/${encodeURIComponent(packetId)}/carry-over`, fromPacketId ? { from_packet_id: fromPacketId } : {});
  return parseCarryResults(body);
}

/** Case-insensitive match on shot id, camera, packet id and tier. Pure. */
export function filterPackets(packets: readonly PacketItem[], query: string): PacketItem[] {
  const q = query.trim().toLowerCase();
  if (q === "") return [...packets];
  return packets.filter((p) => [p.shotId, p.camera, p.packetId, p.tier ? `tier ${p.tier}` : null].some((v) => v?.toLowerCase().includes(q)));
}

/** The link a packet row gives, or the reason it cannot be linked. */
export function linkFor(p: PacketItem): PlatePacketLink | string {
  if (!p.plate) return "the manifest names no display plate";
  if (p.plate.width === null || p.plate.height === null) return "the plate file has no size in the manifest";
  return { packetId: p.packetId, tier: p.tier ?? "A", outputHash: p.plate.blake3, outputSize: { width: p.plate.width, height: p.plate.height } };
}

function PacketThumb({ hash }: { hash: string | null }): ReactElement {
  const thumb = useThumb(hash);
  const box = { width: 64, height: 36, flex: "none", borderRadius: 3, background: C.bg, objectFit: "cover" } as const;
  return thumb.state === "ready" ? <img src={thumb.url} alt="" style={box} /> : <span aria-hidden style={box} />;
}

function PacketRow({ packet, linked, onPick }: { packet: PacketItem; linked: boolean; onPick: (p: PacketItem) => void }): ReactElement {
  const link = linkFor(packet);
  const superseded = packet.supersededBy !== null;
  const blocked = typeof link === "string";
  return (
    <li>
      <button
        type="button"
        data-testid={`plate-packet-${packet.packetId}`}
        data-superseded={superseded}
        aria-pressed={linked}
        disabled={blocked}
        title={blocked ? link : superseded ? `superseded by ${packet.supersededBy}` : `link this plate to ${packet.packetId}`}
        onClick={() => onPick(packet)}
        style={{
          ...E.button,
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          textAlign: "left",
          background: C.bg,
          ...(linked ? { borderColor: C.accent } : null),
          ...(superseded ? { opacity: 0.5 } : null),
          ...(blocked ? E.buttonOff : null),
        }}
      >
        <PacketThumb hash={packet.plate?.blake3 ?? null} />
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={E.row}>
            <strong>{packet.shotId}</strong>
            <Chip tone={packet.tier === "C" ? "advisory" : "info"} title={packet.tier === "C" ? "client plate: image-only gates" : "scene-backed packet: full gates"}>
              Tier {packet.tier ?? "?"}
            </Chip>
            {superseded ? <Chip tone="muted">superseded</Chip> : null}
            {packet.status !== "ingested" ? <Chip tone="fail">{packet.status}</Chip> : null}
          </span>
          <span style={E.note}>
            {packet.camera ?? "camera n/a"} · received {formatTime(packet.receivedUtc)}
            {packet.plate?.width && packet.plate.height ? ` · ${packet.plate.width} x ${packet.plate.height}` : ""}
          </span>
        </span>
      </button>
    </li>
  );
}

function LinkedSummary({ summary }: { summary: PacketSummary }): ReactElement {
  const editable = summary.legend.filter((e) => e.editable).length;
  const locked = summary.legend.filter((e) => e.locked).length;
  return (
    <div style={E.row} data-testid="plate-summary">
      {summary.cameraName ? <Chip tone="muted">camera {summary.cameraName}</Chip> : null}
      {summary.size ? <Chip tone="muted">{`${summary.size.width} x ${summary.size.height}`}</Chip> : null}
      <Chip tone="info" title="Cryptomatte ids in the packet legend">
        {summary.legend.length} ids · {editable} editable · {locked} locked
      </Chip>
    </div>
  );
}

function CarryResults({ from, results }: { from: string; results: CarryResult[] }): ReactElement {
  if (results.length === 0) {
    return (
      <Notice testId="plate-carry-empty">
        nothing to carry: {from} has no accepted layers
      </Notice>
    );
  }
  const carried = results.filter((r) => r.status === "carried").length;
  return (
    <>
      <Chip tone={carried === results.length ? "pass" : "advisory"} testId="plate-carry-count">
        {carried} carried · {results.length - carried} dropped
      </Chip>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }} data-testid="plate-carry-list">
        {results.map((r, i) => (
          <li
            key={`${r.runId ?? "layer"}-${i}`}
            data-testid={`plate-carry-${r.runId ?? i}`}
            data-status={r.status}
            title={r.newRunId ? `new take ${r.newRunId} on this plate: accept it after you check it` : r.code ?? undefined}
            style={E.row}
          >
            <span style={{ color: r.status === "carried" ? TONE_COLOR.pass : TONE_COLOR.advisory }}>{`${r.status} · ${r.reason}`}</span>
            <span style={E.note}>
              {r.ids.length} {r.ids.length === 1 ? "id" : "ids"}
              {r.runId ? ` · run ${r.runId}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

export function PlateEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { node } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const [packets, setPackets] = useState<PacketItem[] | null>(null);
  const [query, setQuery] = useState("");
  const [summary, setSummary] = useState<PacketSummary | null>(null);
  const [carried, setCarried] = useState<{ packetId: string; from: string; results: CarryResult[] } | null>(null);

  const packetId = node?.data.packetId && node.data.packetId !== "unlinked" ? node.data.packetId : null;

  const [refresh, refreshState] = useAction(async (pid: string) => {
    setPackets(await listPackets(pid));
  }, errorMessage);

  useEffect(() => {
    setPackets(null);
    if (projectId) void refresh(projectId);
  }, [projectId, refresh]);

  // live: a packet arriving (or being superseded) for this project re-reads the list
  useEffect(() => {
    if (!projectId) return;
    const handler: RecordHandler = (rec) => {
      if (!rec.project_id || rec.project_id === projectId) void refresh(projectId);
    };
    const stops = PACKET_EVENT_KINDS.map((k) => hub.onKind(k, handler));
    return () => stops.forEach((stop) => stop());
  }, [projectId, refresh]);

  // the linked packet's legend counts; a packet still ingesting (409) or unknown (404) just shows none
  useEffect(() => {
    setSummary(null);
    setCarried(null);
    if (!packetId) return;
    let alive = true;
    packetSummary(packetId).then(
      (s) => alive && setSummary(s),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [packetId]);

  // the predecessor this plate replaces, when the daemon says so: the carry-over button's source
  const predecessor = summary && summary.packetId === packetId ? summary.supersedes : null;
  const [carry, carryState] = useAction(async (pid: string, from: string) => {
    const results = await carryOver(pid, from);
    setCarried({ packetId: pid, from, results });
    if (projectId) await refresh(projectId); // the picker: the new plate's runs and the old one's greying
  }, errorMessage);

  const shown = useMemo(() => filterPackets(packets ?? [], query), [packets, query]);
  const newer = packets?.find((p) => p.packetId === packetId)?.supersededBy ?? null;

  const pick = (p: PacketItem): void => {
    const link = linkFor(p);
    if (typeof link !== "string") useGraphStore.getState().setPlatePacket(nodeId, link);
  };

  return (
    <EditorFrame kind="plate" nodeId={nodeId} title="Plate">
      <div style={E.row}>
        {packetId ? (
          <Chip tone="info" testId="plate-linked">
            packet {packetId} · Tier {node?.data.tier ?? "?"}
          </Chip>
        ) : (
          <Chip tone="advisory" testId="plate-unlinked" title="pick the packet this plate stands for">
            no packet linked
          </Chip>
        )}
        {newer ? (
          <Chip tone="advisory" testId="plate-stale" title="a re-render of this camera arrived; link it to work on the new plate">
            superseded by {newer}
          </Chip>
        ) : null}
      </div>
      {summary && summary.packetId === packetId ? <LinkedSummary summary={summary} /> : null}

      <Section title="Packets" testId="plate-packets">
        <div style={E.row}>
          <Field label="search">
            <input type="search" style={E.wide} value={query} placeholder="shot, camera or packet id" data-testid="plate-search" onChange={(e) => setQuery(e.target.value)} />
          </Field>
          <Button onClick={() => projectId && void refresh(projectId)} disabled={!projectId || refreshState.pending} testId="plate-refresh" title="read the project's packets again">
            {refreshState.pending ? "Loading" : "Refresh"}
          </Button>
        </div>
        {!projectId ? (
          <span style={E.note}>open a project to list its packets</span>
        ) : packets === null ? (
          <span style={E.note}>{refreshState.pending ? "loading the packets" : "no list yet"}</span>
        ) : shown.length === 0 ? (
          <span style={E.note} data-testid="plate-empty">
            {packets.length === 0 ? "no packets in this project yet: send one from Max or import a client plate" : "no packet matches the search"}
          </span>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }} data-testid="plate-list">
            {shown.map((p) => (
              <PacketRow key={p.packetId} packet={p} linked={p.packetId === packetId} onPick={pick} />
            ))}
          </ul>
        )}
      </Section>

      {packetId && predecessor ? (
        <Section title="Client revision" testId="plate-carry">
          <span style={E.note}>this plate replaces {predecessor}: bring the layers you accepted there over; each carried layer is a new take to check and accept</span>
          <div style={E.row}>
            <Button
              onClick={() => void carry(packetId, predecessor)}
              disabled={carryState.pending}
              testId="plate-carry-button"
              title={`re-composite the accepted layers of ${predecessor} onto this plate where their ids are unchanged`}
            >
              {carryState.pending ? "Carrying" : `Carry over approved layers from ${predecessor}`}
            </Button>
          </div>
          {carried && carried.packetId === packetId ? <CarryResults from={carried.from} results={carried.results} /> : null}
          {carryState.error ? (
            <Notice tone="error" testId="plate-carry-error">
              {carryState.error}
            </Notice>
          ) : null}
        </Section>
      ) : null}

      {refreshState.error ? (
        <Notice tone="error" testId="plate-error">
          {refreshState.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}

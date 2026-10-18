// Video player for motion takes (T47): an HTML5 video with exact frame stepping, the time map readout (which packet
// frame an engine frame shows) and the landing / camera-lock / drift chips from the video gate report.
//
//   frame stepping   `,` or Left = one frame back, `.` or Right = one forward, Shift = ten, Space = play / pause
//                    (only while the player has focus, so the viewer's own keys are untouched)
//   time map         `report.metrics.timemap` (TimeMap.to_json): "engine frame 119 -> packet frame 123.96 (nearest 124)"
//   chips            video.first_frame / video.last_frame (levels/255), video.camera_lock_median_flow_px,
//                    video.reproj_edge_recall, coloured by the check's own status
//
// A clip is fetched with the bearer token into a blob URL, because a <video src> cannot send the header. That is a
// whole 4K clip in memory, so a CAS clip loads on request ("Load clip"), not on selection; `src` loads at once.
//
// T106: the picture cannot be saved from the page: `controlsList="nodownload"`, no picture-in-picture and no context
// menu (`NO_DOWNLOAD`, shared with VideoCompare). A take leaves through `Deliver…` (the Export editor's clip formats,
// inline: `POST /runs/{id}/deliver`), which keeps lineage, the gate sidecar and the GATE_FAIL override in the ledger.
// `Compare` mounts VideoCompare on the default pair (the clay reference against the take, else the node's two latest
// sibling clips).

import { type KeyboardEvent, type MouseEvent, type ReactElement, useCallback, useEffect, useRef, useState } from "react";
import { assetUrl } from "../canvas/assets";
import { getToken } from "../api";
import { formatValue } from "../panels/format";
import type { GateStatus } from "../panels/types";
import { ClipDeliver } from "./ExportEditor";
import { C, E, type Tone } from "./theme";
import { covers, frameAt, nearestPacketFrame, packetFrameFor, parseTimeMap, type TimeMap, timeForFrame } from "./timeMap";
import { type CompareSource, defaultPair, VideoCompare } from "./VideoCompare";
import { Button, Chip, Notice } from "./widgets";

/** The slice of a gate report the player reads. */
export interface PlayerReport {
  checks?: { metric_id: string; value: number | null; status: GateStatus }[];
  metrics?: Record<string, unknown>;
  backend_notes?: string[];
}

export interface PlayerChip {
  key: "first" | "last" | "lock" | "drift";
  label: string;
  tone: Tone;
  title: string;
}

const CHIP_SPECS: readonly { key: PlayerChip["key"]; metric: string; label: string; unit: string; note: string }[] = [
  { key: "first", metric: "video.first_frame", label: "first frame", unit: "/255", note: "landing: mean abs error of the first frame against the start still" },
  { key: "last", metric: "video.last_frame", label: "last frame", unit: "/255", note: "landing: mean abs error of the last frame against the end still" },
  { key: "lock", metric: "video.camera_lock_median_flow_px", label: "lock", unit: " px", note: "camera lock: median optical flow at 960 px wide (hard on RAFT, advisory on DIS)" },
  { key: "drift", metric: "video.reproj_edge_recall", label: "drift", unit: "", note: "reprojection drift: worst edge recall against the packet's G-buffer edges" },
];

const STATUS_TONE: Record<GateStatus, Tone> = { pass: "pass", fail: "fail", advisory: "advisory" };

/** The four chips for a report; a metric the report does not hold shows as "not scored". */
export function chipsFromReport(report: PlayerReport | null | undefined): PlayerChip[] {
  return CHIP_SPECS.map((s) => {
    const c = report?.checks?.find((x) => x.metric_id === s.metric);
    if (!c) return { key: s.key, label: `${s.label}: not scored`, tone: "muted", title: s.note };
    return { key: s.key, label: `${s.label} ${formatValue(c.value)}${s.unit}`, tone: STATUS_TONE[c.status], title: `${s.note} (${c.status})` };
  });
}

export const timeMapFromReport = (report: PlayerReport | null | undefined): TimeMap | null => parseTimeMap(report?.metrics?.timemap);

/** Spread on every <video> PLATE shows: no download button, no picture-in-picture, no "Save video as" menu. */
export const NO_DOWNLOAD = {
  controlsList: "nodownload",
  disablePictureInPicture: true,
  onContextMenu: (e: MouseEvent<HTMLVideoElement>): void => e.preventDefault(),
} as const;

/** Fetch a CAS clip with the token into a blob URL; `load()` starts it, the URL is revoked when it changes or unmounts. */
export function useClip(hash: string | null | undefined): { url: string | null; loading: boolean; error: string | null; load: () => void } {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = useRef<string | null>(null);

  const drop = useCallback(() => {
    if (current.current && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(current.current);
    current.current = null;
  }, []);

  useEffect(() => {
    drop();
    setUrl(null);
    setError(null);
    setLoading(false);
    return drop;
  }, [hash, drop]);

  const load = useCallback(() => {
    if (!hash) return;
    setLoading(true);
    setError(null);
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    fetch(assetUrl(hash), { headers })
      .then(async (res) => {
        if (!res.ok) throw new Error(`GET ${assetUrl(hash)} -> ${res.status}`);
        const blob = await res.blob();
        if (typeof URL.createObjectURL !== "function") throw new Error("this browser cannot play a fetched clip");
        drop();
        current.current = URL.createObjectURL(blob);
        setUrl(current.current);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [hash, drop]);

  return { url, loading, error, load };
}

export interface VideoPlayerProps {
  /** CAS hash of the clip. */
  hash?: string | null;
  /** A playable URL; wins over `hash` and loads at once. */
  src?: string | null;
  timeMap?: TimeMap | null;
  report?: PlayerReport | null;
  /** Frame rate when there is no time map. */
  fps?: number;
  /** The take's run: enables `Deliver…`. */
  runId?: string | null;
  /** A GATE_FAIL take without a recorded override: `Deliver…` asks for the reason. */
  needsReason?: boolean;
  /** What `Compare` can pair: the clay reference and the node's takes (see `defaultPair`). */
  compare?: CompareSource | null;
}

export function VideoPlayer({
  hash = null,
  src = null,
  timeMap = null,
  report = null,
  fps: fpsProp = 24,
  runId = null,
  needsReason = false,
  compare = null,
}: VideoPlayerProps): ReactElement {
  const clip = useClip(src ? null : hash);
  const [deliverOpen, setDeliverOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const pair = defaultPair(compare);
  const playable = src ?? clip.url;
  const video = useRef<HTMLVideoElement | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);

  const fps = timeMap?.engineFps ?? fpsProp;
  const total = timeMap?.engineFrames ?? (duration > 0 ? Math.max(1, Math.round(duration * fps)) : 0);
  const frame = frameAt(time, fps);
  const chips = chipsFromReport(report);

  // while playing the readout follows the picture frame by frame
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = (): void => {
      if (video.current) setTime(video.current.currentTime);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const seekFrame = useCallback(
    (f: number): void => {
      const v = video.current;
      if (!v) return;
      const last = total > 0 ? total - 1 : Number.MAX_SAFE_INTEGER;
      const clamped = Math.min(Math.max(0, f), last);
      v.pause();
      v.currentTime = timeForFrame(clamped, fps);
      setTime(v.currentTime);
    },
    [fps, total],
  );

  const step = useCallback((n: number): void => seekFrame(frameAt(video.current?.currentTime ?? time, fps) + n), [seekFrame, fps, time]);

  const toggle = (): void => {
    const v = video.current;
    if (!v) return;
    if (v.paused) void v.play()?.catch(() => undefined);
    else v.pause();
  };

  // Captured, and stopped: the focused <video> has its own Space and arrow-key handling that would act a second time
  const onKey = (e: KeyboardEvent<HTMLDivElement>): void => {
    const big = e.shiftKey ? 10 : 1;
    if (e.key === "," || e.key === "ArrowLeft") step(-big);
    else if (e.key === "." || e.key === "ArrowRight") step(big);
    else if (e.key === " ") toggle();
    else return;
    e.preventDefault();
    e.stopPropagation();
  };

  const nearest = timeMap ? nearestPacketFrame(timeMap, frame) : null;
  const inside = timeMap ? covers(timeMap, frame) : true;

  return (
    <div data-testid="video-player" tabIndex={0} onKeyDownCapture={onKey} style={{ ...E.section, outline: "none" }} aria-label="motion take player">
      {playable ? (
        <video
          ref={video}
          src={playable}
          data-testid="video-element"
          controls
          {...NO_DOWNLOAD}
          muted
          playsInline
          preload="metadata"
          style={{ width: "100%", background: "#000", borderRadius: 4 }}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
          onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
          onSeeked={(e) => setTime(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
        />
      ) : (
        <div style={{ ...E.note, display: "grid", placeItems: "center", minHeight: 72, border: `1px dashed ${C.line}`, borderRadius: 4 }}>
          {hash ? (
            <Button onClick={clip.load} disabled={clip.loading} testId="video-load">
              {clip.loading ? "Loading clip" : "Load clip"}
            </Button>
          ) : (
            <span data-testid="video-empty">no take yet: Make the move to get a clip</span>
          )}
        </div>
      )}
      {clip.error ? (
        <Notice tone="error" testId="video-error">
          {clip.error}
        </Notice>
      ) : null}

      <div style={E.row}>
        <Button onClick={() => step(-10)} disabled={!playable} testId="video-back10" title="ten frames back (Shift+Left)">
          -10
        </Button>
        <Button onClick={() => step(-1)} disabled={!playable} testId="video-back1" title="one frame back (, or Left)">
          frame -
        </Button>
        <Button onClick={toggle} disabled={!playable} testId="video-play">
          {playing ? "Pause" : "Play"}
        </Button>
        <Button onClick={() => step(1)} disabled={!playable} testId="video-fwd1" title="one frame forward (. or Right)">
          frame +
        </Button>
        <Button onClick={() => step(10)} disabled={!playable} testId="video-fwd10" title="ten frames forward (Shift+Right)">
          +10
        </Button>
      </div>

      <div style={E.note} data-testid="video-readout">
        engine frame {frame}
        {total > 0 ? ` / ${total}` : ""} · {(frame / fps).toFixed(3)} s @ {formatValue(fps)} fps
        {timeMap ? (
          <>
            {" "}
            -&gt; packet frame {packetFrameFor(timeMap, frame).toFixed(2)} (nearest {nearest})
            {inside ? "" : " · past the packet's range, clamped"}
          </>
        ) : (
          " · no time map recorded for this clip"
        )}
      </div>
      {timeMap ? (
        <div style={E.note} data-testid="video-timemap">
          time map: packet {formatValue(timeMap.packetFps)} fps, frames {timeMap.start} to {timeMap.end} -&gt; engine {formatValue(timeMap.engineFps)} fps, {timeMap.engineFrames} frames ({timeMap.method})
        </div>
      ) : null}

      <div style={E.row} data-testid="video-chips">
        {chips.map((c) => (
          <Chip key={c.key} tone={c.tone} title={c.title} testId={`video-chip-${c.key}`}>
            {c.label}
          </Chip>
        ))}
      </div>
      {report?.backend_notes && report.backend_notes.length > 0 ? (
        <span style={E.note} data-testid="video-notes">
          {report.backend_notes.slice(0, 3).join(" · ")}
        </span>
      ) : null}

      <div style={E.row}>
        <Button
          onClick={() => setDeliverOpen((o) => !o)}
          disabled={runId === null}
          on={deliverOpen}
          testId="video-deliver"
          title={runId === null ? "no take to deliver yet" : "deliver this take: ProRes 4444, HEVC, EXR sequence or FCPXML, with its lineage and gate sidecar"}
        >
          Deliver…
        </Button>
        <Button
          onClick={() => setCompareOpen((o) => !o)}
          disabled={pair === null}
          on={compareOpen && pair !== null}
          testId="video-compare-toggle"
          title={pair === null ? "nothing to compare yet: a clay reference or a second take of this node" : `${pair[0].label} against ${pair[1].label}, on one clock`}
        >
          Compare
        </Button>
      </div>
      {deliverOpen && runId !== null ? <ClipDeliver runId={runId} needsReason={needsReason} testId="video-deliver-form" /> : null}
      {compareOpen && pair !== null ? <VideoCompare key={`${pair[0].key}|${pair[1].key}`} a={pair[0]} b={pair[1]} /> : null}
    </div>
  );
}

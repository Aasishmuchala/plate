// VideoCompare (T106): two clips on one clock, frame-aligned through each clip's time map. The first clip drives: its
// `timeupdate` (and, while it plays, every animation frame) sets the second's `currentTime` to the frame that shows the
// same packet frame, so a Kling take at 24 fps lines up with a 25 fps clay reference frame for frame.
//
//   align      engine frame jA of A -> packet frame k = packet_frame_for(A, jA) -> B's engine frame
//              jB = round(engine_frame_for(B, k)), clamped to B's frames; a clip without a time map is on the other's
//              packet clock (one frame per packet frame from the same first frame)
//   transport  one Play / Pause (B plays at the rate that keeps it aligned and is re-seated when it drifts half a
//              frame), one scrubber over A's frames, `,` / `.` one frame (Shift: ten), Space play / pause
//   layout     side by side or stacked
//   sides      a label and the landing / lock / drift chips from each side's gate report (read with `GET /runs/{id}/gate`
//              when a side names a run and carries no report)
//
// The default pair (`defaultPair`) is the clay motion reference (the motion packet's clay sequence proxy) against the
// take when there is one, else the node's two latest sibling clips. Both pictures carry `NO_DOWNLOAD`; CAS clips load
// on request (a 4K clip is a lot of memory), a `src` at once.

import { type KeyboardEvent, type ReactElement, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { getGateReport } from "../canvas/runApi";
import { formatValue } from "../panels/format";
import { C, E } from "./theme";
import { engineFrameFor, frameAt, packetFrameFor, type TimeMap, timeForFrame } from "./timeMap";
import { chipsFromReport, NO_DOWNLOAD, type PlayerReport, useClip } from "./VideoPlayer";
import { Button, Chip, Notice } from "./widgets";

/** One side of a comparison. */
export interface CompareClip {
  /** Stable key: the run id of a take, `clay` for the clay reference. */
  key: string;
  label: string;
  kind: "clay" | "take";
  runId?: string | null;
  /** CAS hash of the clip. */
  hash?: string | null;
  /** A playable URL; wins over `hash` and loads at once. */
  src?: string | null;
  timeMap?: TimeMap | null;
  report?: PlayerReport | null;
  /** Frame rate when there is no time map. */
  fps?: number;
}

/** What a Motion node can compare: its clay reference and its takes, newest first; `current` is the take on show. */
export interface CompareSource {
  clay: CompareClip | null;
  takes: readonly CompareClip[];
  current?: string | null;
}

const playable = (c: CompareClip | null | undefined): c is CompareClip => !!c && !!(c.src || c.hash);

/** The clay reference against the take on show, else the two latest takes; null when there is nothing to pair. Pure. */
export function defaultPair(source: CompareSource | null | undefined): [CompareClip, CompareClip] | null {
  if (!source) return null;
  const takes = source.takes.filter(playable);
  const current = takes.find((t) => t.key === source.current) ?? takes[0];
  if (playable(source.clay) && current) return [source.clay, current];
  if (takes.length >= 2) return [takes[0] as CompareClip, takes[1] as CompareClip];
  return null;
}

/** What the clock of one side needs. */
export interface SyncClock {
  timeMap: TimeMap | null;
  /** Frame rate when there is no time map. */
  fps: number;
  /** Frame count when there is no time map, if known (bounds the clamp). */
  frames?: number | null;
}

export const clockFps = (c: SyncClock): number => c.timeMap?.engineFps ?? c.fps;

/** A clip without a time map, on the other side's packet clock: one frame per packet frame from the same first frame. */
const identityMap = (fps: number, start: number, frames: number | null | undefined): TimeMap => ({
  packetFps: fps,
  engineFps: fps,
  start,
  end: frames && frames > 0 ? start + frames - 1 : Number.MAX_SAFE_INTEGER,
  engineFrames: frames && frames > 0 ? frames : Number.MAX_SAFE_INTEGER,
  durationS: frames && frames > 0 ? frames / fps : Number.MAX_SAFE_INTEGER,
  method: "identity",
});

const roundHalfUp = (x: number): number => (x >= 0 ? Math.floor(x + 0.5) : -Math.floor(-x + 0.5));

/** B's engine frame showing the packet frame A shows at its engine frame `jA`. Pure. */
export function alignedFrame(jA: number, a: SyncClock, b: SyncClock): number {
  const mapA = a.timeMap ?? identityMap(a.fps, b.timeMap?.start ?? 0, a.frames);
  const mapB = b.timeMap ?? identityMap(b.fps, mapA.start, b.frames);
  const k = packetFrameFor(mapA, jA);
  const last = (b.timeMap?.engineFrames ?? b.frames ?? Number.MAX_SAFE_INTEGER) - 1;
  return Math.min(Math.max(0, roundHalfUp(engineFrameFor(mapB, k))), Math.max(0, last));
}

/** The `currentTime` B must show while A shows `tA` (the middle of B's aligned frame). Pure. */
export function alignedTime(tA: number, a: SyncClock, b: SyncClock): number {
  return timeForFrame(alignedFrame(frameAt(tA, clockFps(a)), a, b), clockFps(b));
}

/** B's playback rate while A plays at 1x: packet frames per second of A's clip over B's. Pure. */
export function alignedRate(a: SyncClock, b: SyncClock): number {
  // tB = (k - startB) / packetFpsB and k = startA + tA * packetFpsA, so B runs packetFpsA / packetFpsB seconds per second
  const rate = (a.timeMap?.packetFps ?? a.fps) / (b.timeMap?.packetFps ?? b.fps);
  return Number.isFinite(rate) && rate > 0 ? Math.min(Math.max(rate, 0.0625), 16) : 1;
}

export type CompareLayout = "side" | "stacked";

/** The side's report: the one it carries, else its run's stored gate report (read once). */
function useSideReport(clip: CompareClip): PlayerReport | null {
  const [fetched, setFetched] = useState<PlayerReport | null>(null);
  const runId = clip.report ? null : (clip.runId ?? null);
  useEffect(() => {
    setFetched(null);
    if (!runId) return;
    let alive = true;
    getGateReport(runId)
      .then((r) => alive && setFetched(r as unknown as PlayerReport))
      .catch(() => alive && setFetched(null));
    return () => {
      alive = false;
    };
  }, [runId]);
  return clip.report ?? fetched;
}

function Side({
  side,
  clip,
  url,
  videoRef,
  report,
  onTime,
  onPlay,
  onPause,
  onDuration,
}: {
  side: "a" | "b";
  clip: CompareClip;
  url: string | null;
  videoRef: RefObject<HTMLVideoElement | null>;
  report: PlayerReport | null;
  onTime?: () => void;
  onPlay?: () => void;
  onPause?: () => void;
  onDuration: (d: number) => void;
}): ReactElement {
  const chips = chipsFromReport(report);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 0 }} data-testid={`compare-side-${side}`}>
      <div style={E.row}>
        <Chip tone={clip.kind === "clay" ? "info" : "muted"} testId={`compare-label-${side}`} title={clip.kind === "clay" ? "the motion packet's clay sequence: the camera path the artist rendered" : "a take of this node"}>
          {side.toUpperCase()} {clip.label}
        </Chip>
      </div>
      {url ? (
        <video
          ref={videoRef}
          src={url}
          data-testid={`compare-video-${side}`}
          {...NO_DOWNLOAD}
          muted
          playsInline
          preload="metadata"
          style={{ width: "100%", background: "#000", borderRadius: 4 }}
          onLoadedMetadata={(e) => onDuration(e.currentTarget.duration)}
          onTimeUpdate={onTime}
          onSeeked={onTime}
          onPlay={onPlay}
          onPause={onPause}
          onEnded={onPause}
        />
      ) : (
        <div style={{ ...E.note, display: "grid", placeItems: "center", minHeight: 48, border: `1px dashed ${C.line}`, borderRadius: 4 }} data-testid={`compare-empty-${side}`}>
          {clip.hash ? "not loaded" : "no clip"}
        </div>
      )}
      <div style={E.row} data-testid={`compare-chips-${side}`}>
        {chips.map((c) => (
          <Chip key={c.key} tone={c.tone} title={c.title} testId={`compare-chip-${side}-${c.key}`}>
            {c.label}
          </Chip>
        ))}
      </div>
    </div>
  );
}

export function VideoCompare({ a, b, layout: initialLayout = "side" }: { a: CompareClip; b: CompareClip; layout?: CompareLayout }): ReactElement {
  const clipA = useClip(a.src ? null : (a.hash ?? null));
  const clipB = useClip(b.src ? null : (b.hash ?? null));
  const urlA = a.src ?? clipA.url;
  const urlB = b.src ?? clipB.url;
  const reportA = useSideReport(a);
  const reportB = useSideReport(b);
  const va = useRef<HTMLVideoElement | null>(null);
  const vb = useRef<HTMLVideoElement | null>(null);
  const [layout, setLayout] = useState<CompareLayout>(initialLayout);
  const [time, setTime] = useState(0);
  const [durA, setDurA] = useState(0);
  const [durB, setDurB] = useState(0);
  const [playing, setPlaying] = useState(false);

  const fpsA = a.timeMap?.engineFps ?? a.fps ?? 24;
  const fpsB = b.timeMap?.engineFps ?? b.fps ?? 24;
  const totalA = a.timeMap?.engineFrames ?? (durA > 0 ? Math.max(1, Math.round(durA * fpsA)) : 0);
  const totalB = b.timeMap?.engineFrames ?? (durB > 0 ? Math.max(1, Math.round(durB * fpsB)) : null);
  const clockA: SyncClock = { timeMap: a.timeMap ?? null, fps: fpsA, frames: totalA > 0 ? totalA : null };
  const clockB: SyncClock = { timeMap: b.timeMap ?? null, fps: fpsB, frames: totalB };
  // the clocks change only with the clips and their durations; keep the callbacks below stable across renders
  const clocks = useRef({ a: clockA, b: clockB });
  clocks.current = { a: clockA, b: clockB };

  /** Seat B on the frame aligned with A's; within half a frame it is left alone (no seek storm while playing). */
  const follow = useCallback((): void => {
    const A = va.current;
    const B = vb.current;
    if (!A) return;
    setTime(A.currentTime);
    if (!B) return;
    const { a: ca, b: cb } = clocks.current;
    const target = alignedTime(A.currentTime, ca, cb);
    if (Math.abs(B.currentTime - target) > 0.5 / clockFps(cb)) B.currentTime = target;
  }, []);

  // while A plays, timeupdate fires a few times a second: follow it every animation frame
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = (): void => {
      follow();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, follow]);

  const onPlay = (): void => {
    setPlaying(true);
    const B = vb.current;
    if (!B) return;
    B.playbackRate = alignedRate(clocks.current.a, clocks.current.b);
    void B.play()?.catch(() => undefined);
  };

  const onPause = (): void => {
    setPlaying(false);
    vb.current?.pause();
    follow();
  };

  const seekFrame = useCallback(
    (f: number): void => {
      const A = va.current;
      if (!A) return;
      const last = totalA > 0 ? totalA - 1 : Number.MAX_SAFE_INTEGER;
      A.pause();
      vb.current?.pause();
      A.currentTime = timeForFrame(Math.min(Math.max(0, f), last), fpsA);
      follow();
    },
    [totalA, fpsA, follow],
  );

  const frameA = frameAt(time, fpsA);
  const step = (n: number): void => seekFrame(frameAt(va.current?.currentTime ?? time, fpsA) + n);

  const toggle = (): void => {
    const A = va.current;
    if (!A) return;
    if (A.paused) void A.play()?.catch(() => undefined);
    else A.pause();
  };

  // Captured and stopped, as in the player: the focused <video> and the range input handle these keys themselves.
  const onKey = (e: KeyboardEvent<HTMLDivElement>): void => {
    const big = e.shiftKey ? 10 : 1;
    if (e.key === "," || e.key === "ArrowLeft") step(-big);
    else if (e.key === "." || e.key === "ArrowRight") step(big);
    else if (e.key === " ") toggle();
    else return;
    e.preventDefault();
    e.stopPropagation();
  };

  const ready = urlA !== null && urlB !== null;
  const loadable = (!a.src && !!a.hash && !clipA.url) || (!b.src && !!b.hash && !clipB.url);
  const loading = clipA.loading || clipB.loading;
  const error = clipA.error ?? clipB.error;
  const jB = alignedFrame(frameA, clockA, clockB);
  const mapA = a.timeMap ?? null;

  return (
    <div
      data-testid="video-compare"
      data-layout={layout}
      tabIndex={0}
      onKeyDownCapture={onKey}
      aria-label="compare two clips on one clock"
      style={{ ...E.section, outline: "none" }}
    >
      <div style={E.row}>
        <Button onClick={() => setLayout("side")} on={layout === "side"} testId="compare-layout-side">
          side by side
        </Button>
        <Button onClick={() => setLayout("stacked")} on={layout === "stacked"} testId="compare-layout-stacked">
          stacked
        </Button>
        {loadable ? (
          <Button
            onClick={() => {
              if (!a.src && a.hash && !clipA.url) clipA.load();
              if (!b.src && b.hash && !clipB.url) clipB.load();
            }}
            disabled={loading}
            testId="compare-load"
          >
            {loading ? "Loading clips" : "Load clips"}
          </Button>
        ) : null}
      </div>

      <div style={{ display: "flex", flexDirection: layout === "side" ? "row" : "column", gap: 6 }} data-testid="compare-sides">
        <Side
          side="a"
          clip={a}
          url={urlA}
          videoRef={va}
          report={reportA}
          onTime={follow}
          onPlay={onPlay}
          onPause={onPause}
          onDuration={setDurA}
        />
        <Side side="b" clip={b} url={urlB} videoRef={vb} report={reportB} onDuration={setDurB} />
      </div>
      {error ? (
        <Notice tone="error" testId="compare-error">
          {error}
        </Notice>
      ) : null}

      <div style={E.row}>
        <Button onClick={() => step(-1)} disabled={!ready} testId="compare-back1" title="one frame back (,)">
          frame -
        </Button>
        <Button onClick={toggle} disabled={!ready} testId="compare-play">
          {playing ? "Pause" : "Play"}
        </Button>
        <Button onClick={() => step(1)} disabled={!ready} testId="compare-fwd1" title="one frame forward (.)">
          frame +
        </Button>
        <input
          type="range"
          aria-label="shared scrubber"
          data-testid="compare-scrubber"
          min={0}
          max={Math.max(0, totalA - 1)}
          step={1}
          value={Math.min(frameA, Math.max(0, totalA - 1))}
          disabled={!ready || totalA === 0}
          style={{ flex: 1, minWidth: 0 }}
          onChange={(e) => seekFrame(Number(e.target.value))}
        />
      </div>
      <div style={E.note} data-testid="compare-readout">
        A frame {frameA}
        {totalA > 0 ? ` / ${totalA}` : ""}
        {mapA ? ` -> packet frame ${packetFrameFor(mapA, frameA).toFixed(2)}` : ""} · B frame {jB}
        {totalB ? ` / ${totalB}` : ""} · {formatValue(fpsA)} and {formatValue(fpsB)} fps
      </div>
    </div>
  );
}

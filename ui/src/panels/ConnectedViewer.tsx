// <Viewer> wired to the panels: the mode and the gate-flagged tiles come from the panels store ("Open tiles" in the
// gate panel), and every zoom and pan the user makes is folded into the scrub progress of the selected candidate.
// The viewer does not emit a scrub event of its own, so this wrapper derives it from `onChange`, which is the
// "viewer reports the user visited 100% at 4 quadrants" the verdict panel waits for. Mount this instead of a bare
// <Viewer> wherever a candidate is compared with its plate.

import { type ReactElement, useCallback, useEffect, useRef } from "react";
import { type Overlays, Viewer, type ViewerChange, type ViewerProps } from "../viewer";
import { usePanelsStore } from "./store";
import type { ViewSample } from "./scrub";

export type ConnectedViewerProps = Omit<ViewerProps, "mode" | "onChange" | "overlays" | "ref"> & {
  overlays?: Omit<Overlays, "tiles">;
};

export function ConnectedViewer(props: ConnectedViewerProps): ReactElement {
  const { a, zoom, pan } = props;
  const mode = usePanelsStore((s) => s.viewer.mode);
  const tiles = usePanelsStore((s) => s.viewer.tiles);
  const selectedRunId = usePanelsStore((s) => s.selectedRunId);
  const observe = usePanelsStore((s) => s.observeViewer);
  const setViewerMode = usePanelsStore((s) => s.setViewerMode);
  const box = useRef<HTMLDivElement>(null);

  const sample = useCallback(
    (view: ViewSample) => {
      if (!a) return;
      const el = box.current?.querySelector('[data-testid="viewer"]') ?? box.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      observe(view, { width: a.width, height: a.height }, { width: rect.width * dpr, height: rect.height * dpr });
    },
    [a, observe],
  );

  // The viewer reports only user changes, so the first view of each candidate is sampled here.
  useEffect(() => {
    sample({ zoom: zoom ?? "fit", pan: pan ?? null });
  }, [sample, selectedRunId, a?.url, zoom, pan]);

  const onChange = useCallback(
    (c: ViewerChange) => {
      setViewerMode(c.mode);
      sample({ zoom: c.zoom, pan: c.pan });
    },
    [sample, setViewerMode],
  );

  return (
    <div ref={box} data-testid="connected-viewer">
      <Viewer {...props} mode={mode} overlays={{ ...props.overlays, tiles }} onChange={onChange} />
    </div>
  );
}

// Drop-to-restore UI. Listens for an image dropped anywhere in the window (the canvas included), resolves it with
// the daemon and shows the answer above the tabs: "Load N nodes" when a run made the image, a plain note when the
// image is an input or only a near match, and a path box when the browser would not say where the file lives.

import { type CSSProperties, type FormEvent, type ReactElement, useEffect, useRef, useState } from "react";
import { shortId } from "./format";
import { handleDroppedFile, isImageFile, loadDropped, resolveByPath } from "./restore";
import { usePanelsStore } from "./store";
import { C, S } from "./theme";

const overlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 50,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "rgba(10, 132, 255, 0.18)",
  border: `2px dashed ${C.accent}`,
  color: C.text,
  fontSize: 16,
  pointerEvents: "none",
};

const hasFiles = (e: DragEvent): boolean => Array.from(e.dataTransfer?.types ?? []).includes("Files");

/** Window-level drag and drop. An image drop is handled; any other drop is left to the browser. */
export function useImageDrop(): boolean {
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);
  useEffect(() => {
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth.current += 1;
      setDragging(true);
    };
    const onLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    };
    const onOver = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault(); // without this the browser refuses the drop
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setDragging(false);
      const file = Array.from(e.dataTransfer?.files ?? []).find(isImageFile);
      if (file) void handleDroppedFile(file, e.dataTransfer);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("drop", onDrop);
    };
  }, []);
  return dragging;
}

/** A path box: the way in when a drop carried no path, and a plain "resolve by path" entry. */
export function PathResolver({ autoFocus = false }: { autoFocus?: boolean }): ReactElement {
  const [path, setPath] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const p = path.trim();
    if (p) void resolveByPath(p);
  };
  return (
    <form onSubmit={submit} style={S.row} aria-label="Resolve an image by path">
      <input
        style={{ ...S.input, flex: 1 }}
        value={path}
        onChange={(e) => setPath(e.target.value)}
        placeholder="paste the image path"
        aria-label="Image path"
        data-testid="resolve-path"
        autoFocus={autoFocus}
      />
      <button type="submit" style={S.btn} disabled={path.trim() === ""}>
        Resolve
      </button>
    </form>
  );
}

const nodesLabel = (n: number): string => `Load ${n} ${n === 1 ? "node" : "nodes"}`;

export function DropPrompt(): ReactElement | null {
  const drop = usePanelsStore((s) => s.drop);
  const setDrop = usePanelsStore((s) => s.setDrop);
  const dragging = useImageDrop();

  const dismiss = () => setDrop({ kind: "idle" });
  let body: ReactElement | null = null;

  if (drop.kind === "resolving") {
    body = <span role="status">Finding where {drop.name} came from...</span>;
  } else if (drop.kind === "needs-path") {
    body = (
      <span role="status">
        The browser did not say where {drop.name} lives, so the daemon cannot look it up. Paste its path in History.
      </span>
    );
  } else if (drop.kind === "error") {
    body = (
      <span role="alert" style={{ color: C.fail }}>
        {drop.message}
      </span>
    );
  } else if (drop.kind === "match") {
    const { result, run, plan, note } = drop;
    const how =
      result.by === "lineage"
        ? "Matched by the lineage embedded in the file"
        : result.by === "content_hash"
          ? "Matched by exact content hash"
          : `Nearest match by perceptual hash (distance ${result.distance ?? "?"})`;
    body = (
      <>
        <span data-testid="drop-match">
          {how}: asset {shortId(result.content_hash)}
          {result.run_id ? `, run ${shortId(result.run_id)}` : ", no run produced it (a plate or an input)"}.
        </span>
        {note ? (
          <span role="alert" style={{ color: C.fail }}>
            {note}
          </span>
        ) : null}
        {run && !plan ? (
          <span role="status">Run {shortId(run.id)} has no node in this project's graph to restore.</span>
        ) : null}
        {plan ? (
          <button type="button" style={{ ...S.btn, ...S.btnPrimary }} onClick={() => loadDropped()}>
            {nodesLabel(plan.nodeIds.length)}
          </button>
        ) : null}
      </>
    );
  }

  return (
    <>
      {dragging ? (
        <div style={overlay} data-testid="drop-overlay" aria-hidden="true">
          Drop an image to find the run that made it
        </div>
      ) : null}
      {body ? (
        <div style={{ ...S.section, border: `1px solid ${C.accent}` }} data-testid="drop-prompt">
          {body}
          <div style={S.row}>
            <button type="button" style={S.btn} onClick={dismiss}>
              Dismiss
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}

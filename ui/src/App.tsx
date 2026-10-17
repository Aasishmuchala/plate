// Root component. The ReactFlowProvider lives in main.tsx (and in the tests) so App stays provider-agnostic.
// It mounts the three slots later tasks fill: the node editor for the selected node (T47), the viewer (T24, wired
// to the candidate strip by T35: a clicked candidate is compared with its plate) and the panels (T36).
// The canvas theme is imported here (vitest runs with css: false, so tests never load it).

import "./canvas/canvas.css";
import { CompareViewer } from "./canvas/CompareViewer";
import PlateCanvas from "./PlateCanvas";
import { editorFor } from "./editors";
import { Panels } from "./panels";
import { useGraphStore } from "./store";

export default function App() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const Editor = selected ? editorFor(selected.data.kind) : null;
  return (
    <PlateCanvas>
      {Editor && selected ? <Editor nodeId={selected.id} /> : null}
      <CompareViewer />
      <Panels />
    </PlateCanvas>
  );
}

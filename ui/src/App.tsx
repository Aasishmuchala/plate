// Root component. The ReactFlowProvider lives in main.tsx (and in the tests) so App stays provider-agnostic.
// It mounts the three slots later tasks fill: the node editor for the selected node (T47), the viewer (T24) and the
// panels (T36). Each is a placeholder today.

import PlateCanvas from "./PlateCanvas";
import { editorFor } from "./editors";
import { Panels } from "./panels";
import { useGraphStore } from "./store";
import { Viewer } from "./viewer";

export default function App() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const Editor = selected ? editorFor(selected.data.kind) : null;
  return (
    <PlateCanvas>
      {Editor && selected ? <Editor nodeId={selected.id} /> : null}
      <Viewer />
      <Panels />
    </PlateCanvas>
  );
}

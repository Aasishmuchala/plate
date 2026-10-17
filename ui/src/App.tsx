// Root component. The ReactFlowProvider lives in main.tsx (and in the tests) so App stays provider-agnostic.

import PlateCanvas from "./PlateCanvas";

export default function App() {
  return <PlateCanvas />;
}

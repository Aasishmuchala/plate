// The panel area App mounts (T36): drop-to-restore prompt, a tab per panel (projects, history, gate, custody, timer)
// and the verdict keys, which stay visible on every tab because the keys are global. State shared between panels
// lives in `./store`; nothing here owns the graph, which stays in `useGraphStore`.

import { type ReactElement, useEffect } from "react";
import { useGraphStore } from "../store";
import { CustodyPanel } from "./CustodyPanel";
import { DropPrompt } from "./DropRestore";
import { GatePanel } from "./GatePanel";
import { HistoryPanel } from "./HistoryPanel";
import { ProjectPanel } from "./ProjectPanel";
import { type PanelTab, usePanelsStore } from "./store";
import { S } from "./theme";
import { TimerPanel } from "./TimerPanel";
import { VerdictPanel } from "./VerdictPanel";

const TABS: readonly { id: PanelTab; label: string }[] = [
  { id: "project", label: "Projects" },
  { id: "history", label: "History" },
  { id: "gate", label: "Gate" },
  { id: "custody", label: "Custody" },
  { id: "timer", label: "Timer" },
];

function Active({ tab }: { tab: PanelTab }): ReactElement {
  switch (tab) {
    case "project":
      return <ProjectPanel />;
    case "history":
      return <HistoryPanel />;
    case "gate":
      return <GatePanel />;
    case "custody":
      return <CustodyPanel />;
    case "timer":
      return <TimerPanel />;
  }
}

export function Panels(): ReactElement {
  const tab = usePanelsStore((s) => s.tab);
  const setTab = usePanelsStore((s) => s.setTab);
  const loadRuns = usePanelsStore((s) => s.loadRuns);
  const selectRun = usePanelsStore((s) => s.selectRun);
  const projectId = useGraphStore((s) => s.projectId);

  // a different project: its runs, and no candidate carried over from the last one
  useEffect(() => {
    selectRun(null);
    void loadRuns(projectId);
  }, [projectId, loadRuns, selectRun]);

  return (
    <aside className="panels" data-testid="panels" style={S.root}>
      <DropPrompt />
      <div role="tablist" aria-label="Panels" style={S.tabs}>
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`panels-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls="panels-tabpanel"
            style={{ ...S.tab, ...(tab === t.id ? S.tabOn : {}) }}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id="panels-tabpanel" aria-labelledby={`panels-tab-${tab}`}>
        <Active tab={tab} />
      </div>
      <VerdictPanel />
    </aside>
  );
}

export { ConnectedViewer, type ConnectedViewerProps } from "./ConnectedViewer";
export { focusRun, loadDropped, resolveByPath } from "./restore";
export { EMPTY_SCRUB, isFullScrub, observeView, type ScrubProgress } from "./scrub";
export { initialPanelsState, scrubLogFor, usePanelsStore, type ViewerRequest } from "./store";

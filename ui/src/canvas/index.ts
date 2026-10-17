// Canvas runtime (T35): connections with inline refusals, run status over the event bus, the candidate strip,
// the viewer A/B wiring and CAS thumbnails.

export { assetImageSource, assetUrl, clearThumbCache, loadThumb, useThumb } from "./assets";
export { CandidateStrip, compareCandidate, pickCandidate } from "./CandidateStrip";
export { CompareViewer } from "./CompareViewer";
export { attemptConnect, checkConnection, connectionFromDrop, type ConnectVerdict } from "./connect";
export { deriveNodeInfo, type NodeInfo, NodeInfoContext, plateAncestor, useNodeInfo } from "./derive";
export { getGateReport, getRun, overrideRun, UI_ACTOR } from "./runApi";
export {
  applyRecord,
  hub,
  parseRecord,
  projectEventsUrl,
  RunEventHub,
  settleRun,
  unwatchAll,
  unwatchRun,
  watchRun,
} from "./runEvents";
export {
  type Candidate,
  candidateFrom,
  MAX_CANDIDATES,
  nodeCandidates,
  rankCandidates,
  REFUSAL_FLASH_MS,
  useRunStore,
} from "./runStore";
export * from "./runTypes";

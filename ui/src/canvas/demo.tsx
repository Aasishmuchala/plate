// Dev-only demo of the canvas (T35) on a seeded graph with an in-page fake daemon: Plate -> Mask -> Edit with a
// best-of-3 strip (one DONE, one GATE_FAIL, one RUNNING), a Concept node with its watermark, a refused
// Concept -> Export edge flashing on the Export node, and a fresh quote in the Make bar. Serve with
// `npx vite --host 127.0.0.1 --port 5173` from ui/ and open /src/canvas/demo.html. Thumbnails are synthetic
// gradients drawn in the page. `?quote=over` quotes over the auto cap; `?select=none` starts with nothing selected.

import "@xyflow/react/dist/style.css";
import "../index.css";
import { ReactFlowProvider } from "@xyflow/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "../App";
import { useMakeStore } from "../makebar/makeStore";
import { QUOTE_TTL_MS } from "../makebar/quote";
import { useGraphStore } from "../store";
import { attemptConnect } from "./connect";
import { useRunStore } from "./runStore";
import type { QuoteWire, RunWire } from "./runTypes";

const params = new URLSearchParams(location.search);
const price = params.get("quote") === "over" ? 2.4 : 0.034;

function gradient(seed: number): Promise<Blob> {
  const c = document.createElement("canvas");
  c.width = 320;
  c.height = 180;
  const g = c.getContext("2d") as CanvasRenderingContext2D;
  const sky = g.createLinearGradient(0, 0, 0, 180);
  sky.addColorStop(0, `hsl(${205 + seed * 9}, 35%, ${62 - seed * 3}%)`);
  sky.addColorStop(1, `hsl(${30 + seed * 6}, 30%, ${44 + seed * 2}%)`);
  g.fillStyle = sky;
  g.fillRect(0, 0, 320, 180);
  g.fillStyle = "#b9b1a4";
  g.fillRect(70, 60, 180, 120);
  g.fillStyle = "#3a4552";
  for (let x = 85; x < 240; x += 34) for (let y = 75; y < 160; y += 30) g.fillRect(x, y, 18, 16);
  g.fillStyle = `hsl(${110 + seed * 12}, 30%, 32%)`;
  g.beginPath();
  g.ellipse(270, 160, 40 + seed * 4, 28, 0, 0, Math.PI * 2);
  g.fill();
  return new Promise((resolve) => c.toBlob((b) => resolve(b as Blob), "image/png"));
}

const realFetch = window.fetch.bind(window);
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  if (url.includes("/api/v0/assets/")) {
    const seed = Number(/h_(\d)/.exec(url)?.[1] ?? 0);
    return new Response(await gradient(seed), { status: 200, headers: { "Content-Type": "image/png" } });
  }
  if (url.endsWith("/api/v0/health")) return json({ ok: true, daemon_version: "0.1.0", stub_engine: true });
  if (url.endsWith("/api/v0/projects")) return json([{ project_id: "demo", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }]);
  if (url.includes("/api/v0/")) return json({ error: "not_found" }, 404);
  return realFetch(input, init);
};

const now = Date.now();
const quote = (over: Partial<QuoteWire> = {}): QuoteWire => ({
  id: "qt_demo",
  provider: "atlas",
  model: "nano-banana-2.1",
  model_version: "unpinned",
  region: "unknown",
  custody_grade: "D",
  price_usd: price,
  eta_s: 40,
  created_utc: new Date(now).toISOString(),
  expires_utc: new Date(now + QUOTE_TTL_MS).toISOString(),
  price_status: "unconfirmed",
  ...over,
});

useGraphStore.getState().loadProject(
  {
    schema: 1,
    nodes: [
      { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_0", position: { x: 0, y: 40 } },
      { id: "mask-1", kind: "Mask", label: "Mask", params: { ids: ["VEG_shrub_A"] }, position: { x: 300, y: 260 } },
      { id: "edit-1", kind: "Edit", label: "Edit", params: { engine_id: "nano-banana-2.1", task: "vegetation" }, position: { x: 600, y: 20 } },
      { id: "gate-1", kind: "Gate", label: "Gate", gateStatus: "pass", position: { x: 920, y: 20 } },
      { id: "export-1", kind: "Export", label: "Export", position: { x: 1220, y: 40 } },
      { id: "concept-1", kind: "Concept", label: "Concept", position: { x: 920, y: 420 } },
      { id: "motion-1", kind: "Motion", label: "Motion", riskLabel: "high", position: { x: 1220, y: 360 } },
    ],
    edges: [
      { id: "e1", source: "plate-1", target: "mask-1", sourceHandle: "image", targetHandle: "source" },
      { id: "e2", source: "plate-1", target: "edit-1", sourceHandle: "image", targetHandle: "image" },
      { id: "e3", source: "mask-1", target: "edit-1", sourceHandle: "mask", targetHandle: "mask" },
      { id: "e4", source: "edit-1", target: "gate-1", sourceHandle: "image", targetHandle: "candidate" },
      { id: "e5", source: "plate-1", target: "gate-1", sourceHandle: "packet", targetHandle: "reference" },
    ],
  },
  "demo",
);

const runs: RunWire[] = [
  { id: "run_a", state: "DONE", node_id: "edit-1", outputs: ["h_1"], gate_score: 0.93, gate_report: { verdict: "pass" }, sibling_group: "g", sibling_index: 0 },
  {
    id: "run_b",
    state: "GATE_FAIL",
    node_id: "edit-1",
    outputs: ["h_2"],
    gate_score: 0.41,
    gate_report: { verdict: "fail", checks: [{ metric_id: "outside_mask.de00_max", value: 3.2, status: "fail" }] },
    sibling_group: "g",
    sibling_index: 1,
  },
  { id: "run_c", state: "RUNNING", node_id: "edit-1", sibling_group: "g", sibling_index: 2, approved_by: "agent:env_7" },
];
useRunStore.getState().startRuns("edit-1", runs);
useRunStore.getState().pick("edit-1", "run_a");

if (params.get("select") !== "none") {
  useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === "edit-1" })) }));
  const q = quote();
  useMakeStore.setState((s) => ({
    entries: {
      ...s.entries,
      "edit-1": {
        draft: { engineId: "nano-banana-2.1", preset: "default", n: 3, regionCrop: true, params: {} },
        quote: {
          quote: q,
          runs: [0, 1, 2].map((i) => ({ id: `q_${i}`, state: "QUOTED", quote: q })),
          totalUsd: price * 3,
          receivedAt: now,
          ttlMs: QUOTE_TTL_MS,
          autoCapUsd: 2,
        },
        phase: "quoted",
        error: null,
        approvalOpen: params.get("quote") === "over",
        approvalReason: null,
      },
    },
  }));
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </StrictMode>,
);

// A refused edge, so the inline message shows on the Export node.
setTimeout(() => attemptConnect({ source: "concept-1", target: "export-1", sourceHandle: "image", targetHandle: "media" }), 600);

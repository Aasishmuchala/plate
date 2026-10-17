// The shared canvas-rule vectors. T25 writes `spec/canvas_rules_vectors.json` and checks the same file against the
// Python mirror (`daemon/plate_daemon/canvas_rules.py`); this file checks it against the TypeScript rules in
// `canvasRules.ts`. A vector the two sides disagree on is a rule that drifted.
//
// Shape read here (field aliases in brackets are accepted too; snake_case keys are read as camelCase):
//   file     [ vector, ... ]  or  { "vectors" ["cases"]: [ vector, ... ] }
//   edge     { "name", "graph": {"nodes","edges"} (or "nodes"/"edges" at the top), "edge": {"source","target"[,"id"]}
//              ["candidate" | "new_edge" | "connect"], "expect": {"ok": bool, "code"?: RuleCode} ["expected"] }
//   accept   { "name", "node": <node>, "scrub_log": {"fullScrub": bool} | null, "expect": {"ok", "code"?} }
//   depth    { "name", "graph", "node": "<id>", "depth": n }
//   node     { "id", "kind", ...PlateNodeData fields } (the `project.json` wire form) or { "id", "data": {...} }
// A vector that fits none of the shapes fails the test by name, so a format change is loud, not skipped.
// The file is read at build time with `import.meta.glob`, so a checkout without it skips the file-backed block
// (the inline vectors below still run and prove the harness).

import { describe, expect, it } from "vitest";
import { canAccept, generationDepth, type GraphNode, validateEdge } from "./canvasRules";
import { makeNodeData, NODE_KINDS, type NodeKind, type PlateNodeData } from "./nodeTypes";

type Rec = Record<string, unknown>;
const isRecord = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const camel = (k: string): string => k.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());
const camelKeys = (o: Rec): Rec => Object.fromEntries(Object.entries(o).map(([k, v]) => [camel(k), v]));
const first = (o: Rec, ...keys: string[]): unknown => keys.map((k) => o[k]).find((v) => v !== undefined);

function toNode(raw: unknown): GraphNode {
  if (!isRecord(raw)) throw new Error(`node is not an object: ${JSON.stringify(raw)}`);
  const top = camelKeys(raw);
  const fields = isRecord(top.data) ? camelKeys(top.data) : top;
  const kind = fields.kind;
  if (typeof top.id !== "string") throw new Error(`node has no id: ${JSON.stringify(raw)}`);
  if (typeof kind !== "string" || !(NODE_KINDS as readonly string[]).includes(kind)) {
    throw new Error(`node ${top.id} has an unknown kind ${String(kind)}`);
  }
  const { id: _id, position: _position, data: _data, ...data } = fields;
  const pos = isRecord(top.position) ? (top.position as { x: number; y: number }) : { x: 0, y: 0 };
  return { id: top.id, position: pos, data: makeNodeData(kind as NodeKind, data as Partial<PlateNodeData>) };
}

function toGraph(v: Rec): { nodes: GraphNode[]; edges: { id: string; source: string; target: string }[] } {
  const g = isRecord(v.graph) ? v.graph : v;
  const nodes = Array.isArray(g.nodes) ? g.nodes.map(toNode) : [];
  const edges = (Array.isArray(g.edges) ? g.edges : []).map((e) => {
    if (!isRecord(e) || typeof e.source !== "string" || typeof e.target !== "string") {
      throw new Error(`edge is not {source, target}: ${JSON.stringify(e)}`);
    }
    return { id: typeof e.id === "string" ? e.id : `${e.source}->${e.target}`, source: e.source, target: e.target };
  });
  return { nodes, edges };
}

interface Expect {
  ok: boolean;
  code?: string;
}

function toExpect(v: Rec): Expect {
  const e = first(v, "expect", "expected");
  if (typeof e === "string") return e === "ok" ? { ok: true } : { ok: false, code: e };
  if (isRecord(e) && typeof e.ok === "boolean") return { ok: e.ok, code: typeof e.code === "string" ? e.code : undefined };
  throw new Error("no expectation: need expect: {ok, code?}");
}

function toEdge(v: Rec): { id: string; source: string; target: string } | null {
  const raw = first(v, "edge", "candidate", "newEdge", "new_edge", "connect");
  if (raw === undefined) return null;
  if (typeof raw === "string") {
    const [source, target] = raw.split("->").map((s) => s.trim());
    if (source && target) return { id: `${source}->${target}`, source, target };
  } else if (Array.isArray(raw) && raw.length === 2) {
    return { id: `${raw[0]}->${raw[1]}`, source: String(raw[0]), target: String(raw[1]) };
  } else if (isRecord(raw) && typeof raw.source === "string" && typeof raw.target === "string") {
    return { id: typeof raw.id === "string" ? raw.id : `${raw.source}->${raw.target}`, source: raw.source, target: raw.target };
  }
  throw new Error(`cannot read the candidate edge: ${JSON.stringify(raw)}`);
}

/** Null when the TS rules agree with the vector; otherwise why not. Throws on a vector of no known shape. */
export function checkVector(vector: unknown): string | null {
  if (!isRecord(vector)) throw new Error("a vector must be an object");
  const v = camelKeys(vector);
  const edge = toEdge(v);
  if (edge) {
    const want = toExpect(v);
    const got = validateEdge(toGraph(v), edge);
    if (got.ok !== want.ok) return `edge ${edge.id}: expected ok=${want.ok}, got ${JSON.stringify(got)}`;
    if (!got.ok && want.code && got.code !== want.code) return `edge ${edge.id}: expected ${want.code}, got ${got.code}`;
    return null;
  }
  if (v.scrubLog !== undefined || v.accept !== undefined) {
    const want = toExpect(v);
    const log = first(v, "scrubLog", "accept");
    const scrub = isRecord(log) ? (camelKeys(log) as { fullScrub: boolean }) : null;
    const got = canAccept(toNode(v.node), scrub);
    if (got.ok !== want.ok) return `accept: expected ok=${want.ok}, got ${JSON.stringify(got)}`;
    if (!got.ok && want.code && got.code !== want.code) return `accept: expected ${want.code}, got ${got.code}`;
    return null;
  }
  if (typeof v.node === "string" && typeof v.depth === "number") {
    const g = toGraph(v);
    const got = generationDepth(g.nodes, g.edges, v.node);
    return got === v.depth ? null : `depth of ${v.node}: expected ${v.depth}, got ${got}`;
  }
  throw new Error(`no known shape (keys: ${Object.keys(vector).join(", ")})`);
}

function vectorsOf(file: unknown): unknown[] {
  if (Array.isArray(file)) return file;
  if (isRecord(file)) {
    for (const k of ["vectors", "cases"]) if (Array.isArray(file[k])) return file[k] as unknown[];
  }
  throw new Error("the vectors file is neither a list nor {vectors: [...]}");
}

const nameOf = (v: unknown, i: number): string =>
  isRecord(v) && typeof v.name === "string" ? v.name : `vector ${i + 1}`;

// ---------------------------------------------------------------------------------------------------- harness

const INLINE = [
  {
    name: "concept never reaches export",
    nodes: [{ id: "c", kind: "Concept" }, { id: "x", kind: "Export" }],
    edges: [],
    edge: { source: "c", target: "x" },
    expect: { ok: false, code: "CONCEPT_TO_DELIVERY" },
  },
  {
    name: "plate to edit is allowed",
    graph: { nodes: [{ id: "p", kind: "Plate", packet_id: "room_a", tier: "A" }, { id: "e", kind: "Edit" }], edges: [] },
    edge: ["p", "e"],
    expected: "ok",
  },
  {
    name: "video stays locked until the still is approved",
    nodes: [
      { id: "p", kind: "Plate", packetId: "room_a", tier: "A" },
      { id: "e", kind: "Edit", gate_status: "pass", approved: false },
      { id: "m", kind: "Motion" },
    ],
    edges: [{ id: "p->e", source: "p", target: "e" }],
    edge: "e->m",
    expect: { ok: false, code: "VIDEO_LOCKED" },
  },
  {
    name: "accept needs a full scrub",
    node: { id: "e", kind: "Edit" },
    scrub_log: null,
    expect: { ok: false, code: "SCRUB_REQUIRED" },
  },
  {
    name: "depth counts generators from the plate",
    nodes: [{ id: "p", kind: "Plate", packetId: "a", tier: "A" }, { id: "e", kind: "Edit" }, { id: "g", kind: "Gate" }],
    edges: [{ source: "p", target: "e" }, { source: "e", target: "g" }],
    node: "g",
    depth: 1,
  },
];

describe("vector harness (inline vectors)", () => {
  for (const [i, v] of INLINE.entries()) {
    it(nameOf(v, i), () => {
      expect(checkVector(v)).toBeNull();
    });
  }

  it("reports a disagreement instead of passing it", () => {
    const wrong = { ...INLINE[0], expect: { ok: true } };
    expect(checkVector(wrong)).toMatch(/expected ok=true/);
    const wrongCode = { ...INLINE[0], expect: { ok: false, code: "CYCLE" } };
    expect(checkVector(wrongCode)).toMatch(/expected CYCLE, got CONCEPT_TO_DELIVERY/);
  });

  it("refuses a vector of no known shape", () => {
    expect(() => checkVector({ name: "mystery", foo: 1 })).toThrow(/no known shape/);
  });
});

// ---------------------------------------------------------------------------------------------------- the file

const FILES = import.meta.glob("../../spec/canvas_rules_vectors.json", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const RAW = Object.values(FILES)[0];

describe.skipIf(RAW === undefined)("spec/canvas_rules_vectors.json against the TypeScript rules", () => {
  const vectors = RAW === undefined ? [] : vectorsOf(JSON.parse(RAW));

  it("holds at least one vector", () => {
    expect(vectors.length).toBeGreaterThan(0);
  });

  for (const [i, v] of vectors.entries()) {
    it(nameOf(v, i), () => {
      expect(checkVector(v)).toBeNull();
    });
  }
});

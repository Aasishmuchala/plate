// Canvas menus (T98). Right-click a node: Open in viewer, Set as A / Set as B, Duplicate, Focus, Copy prompt, Copy
// output hash, Delete (on a selected node these act on the whole selection). Right-click the empty canvas: Add node
// here (a submenu of the palette kinds), Fit, Tidy, Clear graph (asked again inside the menu, then saved at once).
// Drag a wire out of an output and let go on the empty canvas: a picker of the kinds whose first input takes that
// output's type (any input, when no first input does); a pick adds the node at the drop point and wires it through
// the same checks as a hand-made edge, and a kind the canvas rules would refuse is listed disabled with the reason.
//
// The viewer's A/B is T101's `runStore.setCompareSide(side, ref)`; until it exists, Set as A / Set as B are hidden
// and Open in viewer only offers itself for a node with a run (it compares that run with its plate, as a click on
// the candidate strip does). One menu is open at a time (`useCanvasMenu`); it is portalled to the app root
// (`.plate-app`, so the theme and its buttons apply; the body when there is none) at the pointer, kept on screen (a
// submenu opens upwards or to the left when it must), and closes on Escape, on a press outside it, on resize, and
// when the view pans or zooms (PlateCanvas).

import { type FinalConnectionState, useReactFlow } from "@xyflow/react";
import { type KeyboardEvent, type ReactElement, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { create } from "zustand";
import { type GraphNode, validateEdge } from "../canvasRules";
import { type HandleType, makeNodeData, type NodeKind, type PlateNodeData } from "../nodeTypes";
import { acceptsOf, checkHandles, edgeType, PORTS } from "../nodes/ports";
import { useGraphStore } from "../store";
import { compareCandidate } from "./CandidateStrip";
import { attemptConnect } from "./connect";
import { FIT_VIEW, focusNodes } from "./layout";
import { type Candidate, nodeCandidates, useRunStore } from "./runStore";

// ---------------------------------------------------------------------------- which menu is open

/** Client (screen) coordinates of the press that opened a menu. */
export type CanvasMenu =
  | { kind: "node"; nodeId: string; x: number; y: number }
  | { kind: "pane"; x: number; y: number }
  | { kind: "drop"; x: number; y: number; source: string; sourceHandle: string | null; type: HandleType };

interface CanvasMenuStore {
  menu: CanvasMenu | null;
  /** Bumped on every open, so a menu opened over another one starts fresh. */
  seq: number;
  openMenu: (menu: CanvasMenu) => void;
  closeMenu: () => void;
}

export const useCanvasMenu = create<CanvasMenuStore>()((set) => ({
  menu: null,
  seq: 0,
  openMenu: (menu) => set((s) => ({ menu, seq: s.seq + 1 })),
  closeMenu: () => set((s) => (s.menu === null ? s : { menu: null })),
}));

// ---------------------------------------------------------------------------- the viewer seam (T101)

export type CompareSide = "a" | "b";
export interface CompareRef {
  kind: "run" | "plate" | "node";
  runId?: string;
  nodeId?: string;
}
type SetCompareSide = (side: CompareSide, ref: CompareRef) => void;

/** T101's `useRunStore.getState().setCompareSide`, or null while it does not exist. */
export function compareSideSetter(): SetCompareSide | null {
  const fn = (useRunStore.getState() as unknown as { setCompareSide?: unknown }).setCompareSide;
  return typeof fn === "function" ? (fn as SetCompareSide) : null;
}

/** The run a node shows: its picked candidate, else the first in strip order. */
function shownRun(nodeId: string): Candidate | null {
  const runs = useRunStore.getState();
  const cands = nodeCandidates(runs, nodeId);
  const picked = runs.byNode[nodeId]?.picked;
  return cands.find((c) => c.runId === picked) ?? cands[0] ?? null;
}

const nodeData = (nodeId: string): PlateNodeData | null =>
  useGraphStore.getState().nodes.find((n) => n.id === nodeId)?.data ?? null;

/** What Set as A / Set as B put in the viewer for a node: its run, else its output (a Plate's beauty). */
export function compareRefFor(nodeId: string): CompareRef | null {
  const run = shownRun(nodeId);
  if (run) return { kind: "run", runId: run.runId, nodeId };
  const data = nodeData(nodeId);
  if (!data?.outputHash) return null;
  return { kind: data.kind === "Plate" ? "plate" : "node", nodeId };
}

/** Open in viewer has something to show. */
export function canOpenInViewer(nodeId: string): boolean {
  if (shownRun(nodeId)) return true;
  return compareSideSetter() !== null && compareRefFor(nodeId) !== null;
}

/** A node with a run: compare that run with its plate (as the candidate strip does). Otherwise, once T101's A/B
 * exists, show the node's output alone (as A, nothing as B). Returns false when there is nothing to show. */
export function openInViewer(nodeId: string): boolean {
  const run = shownRun(nodeId);
  if (run) {
    compareCandidate(nodeId, run.runId);
    return true;
  }
  const setSide = compareSideSetter();
  const ref = compareRefFor(nodeId);
  if (!setSide || !ref) return false;
  useRunStore.getState().closeCompare();
  setSide("a", ref);
  return true;
}

// ---------------------------------------------------------------------------- node facts

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() !== "" ? v.trim() : null);

/** The prompt a node runs with: `params.prompt`, else its recipe id. */
export function promptOf(data: PlateNodeData): { prompt: string } | { recipe: string } | null {
  const prompt = text(data.params?.prompt);
  if (prompt) return { prompt };
  const recipe = text(data.params?.recipe);
  return recipe ? { recipe } : null;
}

async function copyText(value: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------- the connect-drop picker

/** The kinds a dropped `type` wire can start: those whose first input takes it; if none, those with any input that
 * does (a mask or a reference has no kind that takes it first). In `kinds` order. */
export function dropKinds(type: HandleType, kinds: readonly NodeKind[]): NodeKind[] {
  const first = kinds.filter((k) => {
    const inp = PORTS[k].inputs[0];
    return inp !== undefined && acceptsOf(inp).includes(type);
  });
  if (first.length > 0) return first;
  return kinds.filter((k) => PORTS[k].inputs.some((p) => acceptsOf(p).includes(type)));
}

/** Would a new `kind` node accept the wire from `source`/`sourceHandle`? The same two checks a drop on a handle gets. */
export function dropVerdict(
  source: string,
  sourceHandle: string | null,
  kind: NodeKind,
): { ok: true } | { ok: false; message: string } {
  const { nodes, edges } = useGraphStore.getState();
  const from = nodes.find((n) => n.id === source);
  if (!from) return { ok: false, message: "unknown node" };
  const typed = checkHandles(from.data.kind, sourceHandle, kind, null);
  if (!typed.ok) return { ok: false, message: typed.message };
  const probe: GraphNode = { id: "__drop__", position: { x: 0, y: 0 }, data: makeNodeData(kind) };
  const verdict = validateEdge(
    { nodes: [...nodes, probe], edges },
    { id: "__drop__", source, target: probe.id, sourceHandle: typed.sourceHandle, targetHandle: typed.targetHandle },
  );
  return verdict.ok ? { ok: true } : { ok: false, message: verdict.message };
}

const clientPoint = (e: MouseEvent | TouchEvent): { x: number; y: number } | null => {
  if ("changedTouches" in e) {
    const t = e.changedTouches[0];
    return t ? { x: t.clientX, y: t.clientY } : null;
  }
  return { x: e.clientX, y: e.clientY };
};

/**
 * xyflow's `onConnectEnd` for a wire let go on the empty canvas (no handle under it, not on a node): open the
 * picker there. Returns false for any other drop (a drop on a handle is xyflow's or `connectionFromDrop`'s).
 */
export function openDropPicker(event: MouseEvent | TouchEvent, state: FinalConnectionState): boolean {
  if (state.isValid === true || state.toHandle || !state.fromHandle || state.fromHandle.type !== "source") return false;
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest(".react-flow__node")) return false;
  const source = state.fromHandle.nodeId;
  const kind = useGraphStore.getState().nodes.find((n) => n.id === source)?.data.kind;
  const type = edgeType(kind, state.fromHandle.id);
  const at = clientPoint(event);
  if (!type || !at) return false;
  useCanvasMenu.getState().openMenu({ kind: "drop", ...at, source, sourceHandle: state.fromHandle.id ?? null, type });
  return true;
}

// ---------------------------------------------------------------------------- the menu

export interface MenuItem {
  /** `data-testid="menu-<id>"`. */
  id: string;
  label: string;
  onSelect?: () => void;
  disabled?: boolean;
  /** Tooltip: why it is disabled, or what it does. */
  hint?: string;
  danger?: boolean;
  submenu?: readonly MenuItem[];
  /** Asked inside the menu before `onSelect` runs. */
  confirm?: { question: string; action: string };
}

export interface ContextMenuProps {
  x: number;
  y: number;
  title?: string;
  items: readonly MenuItem[];
  onClose: () => void;
  testId?: string;
}

const MENU_STYLE = {
  position: "fixed",
  zIndex: 1000,
  minWidth: 180,
  padding: 4,
  borderRadius: 8,
  background: "var(--panel, #2c2c2e)",
  border: "1px solid var(--hair, rgba(255, 255, 255, 0.1))",
  boxShadow: "0 8px 24px rgba(0, 0, 0, 0.45)",
  color: "var(--text, #f2f2f7)",
  font: '12.5px "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif',
} as const;

const ITEM_STYLE = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 12,
  width: "100%",
  padding: "5px 10px",
  border: "none",
  borderRadius: 5,
  background: "transparent",
  color: "inherit",
  font: "inherit",
  textAlign: "left",
  cursor: "pointer",
} as const;

const TITLE_STYLE = { padding: "4px 10px 6px", color: "var(--dim, #98989d)", fontSize: 11, letterSpacing: "0.04em" } as const;

function itemStyle(item: MenuItem, active: boolean) {
  return {
    ...ITEM_STYLE,
    ...(item.disabled ? { color: "var(--faint, #636366)", cursor: "default" } : {}),
    ...(item.danger && !item.disabled ? { color: "var(--red, #ff453a)" } : {}),
    ...(active ? { background: "var(--fill, #3a3a3c)" } : {}),
  };
}

const SUBMENU_STYLE = {
  ...MENU_STYLE,
  position: "absolute",
  left: "100%",
  top: 0,
  maxHeight: "min(560px, calc(100vh - 16px))",
  overflowY: "auto",
} as const;

type Placement = { top: number; left?: string; right?: string };

/** Where a submenu goes so it stays on screen: up by its overflow at the bottom, to the left of its item when the
 * right side has no room. */
function useOnScreen(ref: { current: HTMLElement | null }, enabled: boolean): Placement {
  const [place, setPlace] = useState<Placement>({ top: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!enabled || !el) return;
    const r = el.getBoundingClientRect();
    const h = window.innerHeight;
    const w = window.innerWidth;
    const top = r.bottom > h - 4 ? -Math.min(r.bottom - h + 4, Math.max(0, r.top - 4)) : 0;
    setPlace(r.right > w - 4 && r.left - r.width > 4 ? { top, left: "auto", right: "100%" } : { top });
  }, [ref, enabled]);
  return place;
}

function MenuList({
  items,
  openSub,
  setOpenSub,
  choose,
  testId,
  sub,
}: {
  items: readonly MenuItem[];
  openSub: string | null;
  setOpenSub: (id: string | null) => void;
  choose: (item: MenuItem) => void;
  testId: string;
  sub?: boolean;
}): ReactElement {
  const ref = useRef<HTMLDivElement>(null);
  const place = useOnScreen(ref, sub === true);
  // the item under the pointer or the keyboard focus is lit (inline styles have no :hover)
  const [hot, setHot] = useState<string | null>(null);
  const cool = (id: string) => setHot((h) => (h === id ? null : h));
  return (
    <div ref={ref} role="menu" data-testid={testId} style={sub ? { ...SUBMENU_STYLE, ...place } : undefined}>
      {items.map((item) => (
        <div
          key={item.id}
          style={{ position: "relative" }}
          onMouseEnter={sub ? undefined : () => setOpenSub(item.submenu ? item.id : null)}
        >
          <button
            type="button"
            role="menuitem"
            data-testid={`menu-${item.id}`}
            disabled={item.disabled}
            aria-haspopup={item.submenu ? "menu" : undefined}
            aria-expanded={item.submenu ? openSub === item.id : undefined}
            title={item.hint}
            style={itemStyle(item, !item.disabled && (hot === item.id || openSub === item.id))}
            onMouseEnter={() => setHot(item.id)}
            onMouseLeave={() => cool(item.id)}
            onFocus={() => setHot(item.id)}
            onBlur={() => cool(item.id)}
            onClick={() => choose(item)}
          >
            <span>{item.label}</span>
            {item.submenu ? <span aria-hidden="true">›</span> : null}
          </button>
          {item.submenu && openSub === item.id ? (
            <MenuList
              items={item.submenu}
              openSub={null}
              setOpenSub={() => {}}
              choose={choose}
              testId={`submenu-${item.id}`}
              sub
            />
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** A small menu at (x, y), portalled to the app root. Arrow keys move between items; Escape closes it. */
export function ContextMenu({ x, y, title, items, onClose, testId = "context-menu" }: ContextMenuProps): ReactElement {
  const ref = useRef<HTMLDivElement>(null);
  const [openSub, setOpenSub] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<MenuItem | null>(null);
  const [at, setAt] = useState({ x, y });

  // keep the menu on screen
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const w = window.innerWidth;
    const h = window.innerHeight;
    setAt({
      x: x + r.width > w ? Math.max(4, w - r.width - 4) : x,
      y: y + r.height > h ? Math.max(4, h - r.height - 4) : y,
    });
  }, [x, y]);

  // first item focused, so the keys work at once
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [confirming]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onPress = (e: Event) => {
      if (!(e.target instanceof Node) || !ref.current?.contains(e.target)) onClose();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPress, true);
    document.addEventListener("mousedown", onPress, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPress, true);
      document.removeEventListener("mousedown", onPress, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  // give the focus back to whatever had it (the canvas, a node) when the menu goes
  useEffect(() => {
    const prev = document.activeElement;
    return () => {
      if (prev instanceof HTMLElement && prev !== document.body && prev.isConnected) prev.focus();
    };
  }, []);

  const choose = (item: MenuItem) => {
    if (item.disabled) return;
    if (item.submenu) {
      setOpenSub(item.id);
      return;
    }
    if (item.confirm) {
      setConfirming(item);
      return;
    }
    onClose();
    item.onSelect?.();
  };

  // the canvas keys (z, Cmd/Ctrl+D, Delete) and the verdict keys stay out of the menu
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const buttons = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    if (buttons.length === 0) return;
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === "ArrowDown" ? (i + 1) % buttons.length : (i - 1 + buttons.length) % buttons.length;
    buttons[next].focus();
  };

  return createPortal(
    <div
      ref={ref}
      className="canvas-menu"
      data-testid={testId}
      style={{ ...MENU_STYLE, left: at.x, top: at.y }}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => e.preventDefault()}
    >
      {title ? <div style={TITLE_STYLE}>{title}</div> : null}
      {confirming?.confirm ? (
        <div
          role="alertdialog"
          aria-label={confirming.label}
          data-testid="menu-confirm-box"
          style={{ padding: "4px 6px", maxWidth: 260 }}
        >
          <p style={{ margin: "2px 4px 8px", lineHeight: 1.4 }}>{confirming.confirm.question}</p>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <button type="button" className="btn btn-ghost" data-testid="menu-cancel" onClick={() => setConfirming(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn"
              data-testid="menu-confirm"
              style={{ color: "var(--red, #ff453a)" }}
              onClick={() => {
                onClose();
                confirming.onSelect?.();
              }}
            >
              {confirming.confirm.action}
            </button>
          </div>
        </div>
      ) : (
        <MenuList items={items} openSub={openSub} setOpenSub={setOpenSub} choose={choose} testId={`${testId}-items`} />
      )}
    </div>,
    document.querySelector(".plate-app") ?? document.body,
  );
}

// ---------------------------------------------------------------------------- the three canvas menus

const plural = (n: number, one: string): string => (n === 1 ? one : `${one} ${n} nodes`);
const count = (n: number, noun: string): string => `${n} ${noun}${n === 1 ? "" : "s"}`;

/** The open canvas menu, if any. Render inside the ReactFlow tree (it reads the view to place and focus nodes). */
export function CanvasMenus({ kinds }: { kinds: readonly NodeKind[] }): ReactElement | null {
  const menu = useCanvasMenu((s) => s.menu);
  const seq = useCanvasMenu((s) => s.seq);
  const close = useCanvasMenu((s) => s.closeMenu);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const flow = useReactFlow();

  // a node menu whose node is gone (deleted by a key, a reload) closes
  const node = menu?.kind === "node" ? (nodes.find((n) => n.id === menu.nodeId) ?? null) : null;
  useEffect(() => {
    if (menu?.kind === "node" && node === null) close();
  }, [menu, node, close]);
  if (!menu || (menu.kind === "node" && !node)) return null;

  const graph = useGraphStore.getState;
  const flowAt = () => flow.screenToFlowPosition({ x: menu.x, y: menu.y });
  let items: MenuItem[];
  let title: string | undefined;

  if (menu.kind === "node" && node) {
    const targets = node.selected ? nodes.filter((n) => n.selected).map((n) => n.id) : [node.id];
    const setSide = compareSideSetter();
    const ref = compareRefFor(node.id);
    const prompt = promptOf(node.data);
    const hash = node.data.outputHash;
    title = node.data.label === node.data.kind ? node.id : `${node.data.label} · ${node.id}`;
    items = [
      ...(canOpenInViewer(node.id) ? [{ id: "open", label: "Open in viewer", onSelect: () => void openInViewer(node.id) }] : []),
      ...(setSide && ref
        ? [
            { id: "set-a", label: "Set as A", onSelect: () => setSide("a", ref) },
            { id: "set-b", label: "Set as B", onSelect: () => setSide("b", ref) },
          ]
        : []),
      {
        id: "duplicate",
        label: plural(targets.length, "Duplicate"),
        hint: "Cmd/Ctrl+D",
        onSelect: () => void graph().duplicateNodes(targets),
      },
      { id: "focus", label: "Focus", hint: "z", onSelect: () => focusNodes(flow, targets) },
      {
        id: "copy-prompt",
        label: "Copy prompt",
        disabled: !prompt,
        hint: !prompt
          ? "no prompt or recipe on this node"
          : "recipe" in prompt
            ? `copies the recipe id ${prompt.recipe}`
            : undefined,
        onSelect: () => void copyText(prompt ? ("prompt" in prompt ? prompt.prompt : prompt.recipe) : ""),
      },
      {
        id: "copy-hash",
        label: "Copy output hash",
        disabled: !hash,
        hint: hash ?? "no output yet",
        onSelect: () => void copyText(hash ?? ""),
      },
      {
        id: "delete",
        label: plural(targets.length, "Delete"),
        hint: "Delete or Backspace",
        danger: true,
        onSelect: () => {
          for (const id of targets) graph().removeNode(id);
        },
      },
    ];
  } else if (menu.kind === "pane") {
    const empty = nodes.length === 0 && edges.length === 0;
    items = [
      {
        id: "add",
        label: "Add node here",
        submenu: kinds.map((k) => ({ id: `add-${k}`, label: k, onSelect: () => void graph().addNode(k, flowAt()) })),
      },
      {
        id: "fit",
        label: "Fit",
        disabled: nodes.length === 0,
        onSelect: () => void flow.fitView({ ...FIT_VIEW, duration: 300 }),
      },
      {
        id: "tidy",
        label: "Tidy",
        disabled: nodes.length === 0,
        hint: "lay out by rank from the plates",
        onSelect: () => void graph().tidy(),
      },
      {
        id: "clear",
        label: "Clear graph…",
        danger: true,
        disabled: empty,
        confirm: {
          question: `Remove all ${count(nodes.length, "node")} and ${count(edges.length, "edge")}? The empty graph is saved at once.`,
          action: "Clear graph",
        },
        onSelect: () => void graph().clearGraph(),
      },
    ];
  } else if (menu.kind === "drop") {
    const { source, sourceHandle, type } = menu;
    const offered = dropKinds(type, kinds);
    title = `Add and connect · ${type}`;
    items =
      offered.length === 0
        ? [{ id: "none", label: `nothing takes ${type}`, disabled: true }]
        : offered.map((k) => {
            const verdict = dropVerdict(source, sourceHandle, k);
            return {
              id: `drop-${k}`,
              label: k,
              disabled: !verdict.ok,
              hint: verdict.ok ? `add a ${k} and wire it` : verdict.message,
              onSelect: () => {
                const p = flowAt();
                const id = graph().addNode(k, { x: p.x, y: p.y - 40 });
                attemptConnect({ source, sourceHandle, target: id, targetHandle: null });
              },
            };
          });
  } else {
    return null;
  }

  return (
    <ContextMenu key={seq} x={menu.x} y={menu.y} title={title} items={items} onClose={close} testId={`canvas-menu-${menu.kind}`} />
  );
}

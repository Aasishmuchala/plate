import { ReactFlowProvider } from "@xyflow/react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { TOKEN_KEY } from "./api/client";
import { editorFor } from "./editors";
import { NODE_KINDS } from "./nodeTypes";
import { createInitialGraph, useGraphStore } from "./store";

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ ok: true, daemon_version: "0.1.0", stub_engine: true }), { status: 200 })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

const mount = () =>
  render(
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>,
  );

describe("App", () => {
  it("renders the placeholder canvas without throwing and lists Plate, Comp, Gate", () => {
    mount();
    const items = screen.getAllByTestId("node-item").map((li) => li.textContent);
    expect(items).toEqual(["Plate", "Comp", "Gate"]);
  });

  it("shows the Make bar as a pending quote, never a price", () => {
    mount();
    const bar = screen.getByTestId("make-bar");
    expect(bar).toHaveTextContent("provider");
    expect(bar).toHaveTextContent("custody");
    expect(bar).toHaveTextContent("quote pending");
  });

  it("shows the last refusal in the node list", () => {
    useGraphStore.setState({ lastRefusal: "cycle" });
    mount();
    expect(screen.getByTestId("last-refusal")).toHaveTextContent("cycle");
  });

  it("mounts the viewer and panels placeholders", () => {
    mount();
    expect(screen.getByTestId("viewer")).toBeInTheDocument();
    expect(screen.getByTestId("panels")).toBeInTheDocument();
  });

  it("shows how many loaded edges break a canvas rule", () => {
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { kind: "Concept", id: "c", label: "Concept" },
        { kind: "Export", id: "x", label: "Export" },
      ],
      edges: [{ id: "c->x", source: "c", target: "x", sourceHandle: null, targetHandle: null }],
    });
    mount();
    expect(screen.getByTestId("violations")).toHaveTextContent("1 edge breaks the canvas rules");
  });
});

describe("status bar", () => {
  it("shows the daemon as up with its version and the token as present", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    mount();
    await waitFor(() => expect(screen.getByTestId("status-daemon")).toHaveAttribute("data-state", "up"));
    expect(screen.getByTestId("status-daemon")).toHaveTextContent("up v0.1.0 (stub engine)");
    expect(screen.getByTestId("status-token")).toHaveTextContent("token: present");
  });

  it("shows the daemon as unreachable and the token as missing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("offline"))));
    mount();
    await waitFor(() => expect(screen.getByTestId("status-daemon")).toHaveAttribute("data-state", "down"));
    expect(screen.getByTestId("status-daemon")).toHaveTextContent("unreachable");
    expect(screen.getByTestId("status-token")).toHaveTextContent("token: missing");
  });
});

describe("placeholders for later tasks", () => {
  it("editorFor returns null for every kind until T47 replaces it", () => {
    for (const kind of NODE_KINDS) expect(editorFor(kind)).toBeNull();
  });
});

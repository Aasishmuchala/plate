// T99: the Recomposite bar and its route client. The daemon is a fake `fetch`; no WebGL, no event stream.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  checkRecomposite,
  FEATHER_MAX,
  FEATHER_MIN,
  GROW_MAX,
  GROW_MIN,
  recomposite,
} from "../canvas/recompositeApi";
import { type CandidateRun, useRunStore } from "../canvas/runStore";
import { RecompositeBar } from "../editors/RecompositeBar";
import { initialPanelsState, usePanelsStore } from "../panels/store";

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown);

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers "METHOD /path" (the /api/v0 prefix stripped); anything else is a recorded 404. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      const call: Call = { method, path, body };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      if (route === undefined) return json({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : json(out);
    }),
  );
  return calls;
}

const RAW = "a".repeat(64);

/** The run view `POST /runs/{id}/recomposite` answers: a finished, free run on the local route. */
const made = (id: string, over: Partial<CandidateRun> = {}): CandidateRun => ({
  id,
  state: "DONE",
  node_id: "edit-1",
  outputs: [`h_${id}`],
  route: { engine_id: "nano-banana-2.1" },
  attempt: 1,
  quote: {
    id: "qt1",
    provider: "local-recomposite",
    model: "recomposite",
    custody_grade: "C0",
    price_usd: 0,
    eta_s: 5,
    created_utc: "2026-10-18T00:00:00Z",
    expires_utc: "2026-10-18T00:15:00Z",
  },
  ...over,
});

const type = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });

beforeEach(() => {
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkRecomposite", () => {
  it("holds the daemon's limits", () => {
    expect(checkRecomposite({})).toBeNull();
    expect(checkRecomposite({ feather_px: FEATHER_MIN, grow_px: GROW_MIN })).toBeNull();
    expect(checkRecomposite({ feather_px: FEATHER_MAX, grow_px: GROW_MAX, mask_hash: RAW })).toBeNull();
    expect(checkRecomposite({ feather_px: FEATHER_MIN - 1 })).toMatch(/feather/);
    expect(checkRecomposite({ feather_px: FEATHER_MAX + 0.5 })).toMatch(/feather/);
    expect(checkRecomposite({ grow_px: GROW_MAX + 1 })).toMatch(/grow/);
    expect(checkRecomposite({ grow_px: GROW_MIN - 1 })).toMatch(/grow/);
    expect(checkRecomposite({ grow_px: 1.5 })).toMatch(/grow/);
    expect(checkRecomposite({ mask_hash: "xyz" })).toMatch(/mask_hash/);
  });

  it("refuses a bad request without calling the daemon", async () => {
    const calls = mockApi();
    await expect(recomposite("r1", { grow_px: 99 })).rejects.toThrow(/grow/);
    expect(calls).toHaveLength(0);
  });
});

describe("RecompositeBar", () => {
  it("renders only for a take that kept its raw layer", () => {
    const { rerender } = render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={null} />);
    expect(screen.queryByTestId("recomposite-bar")).toBeNull();
    rerender(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);
    expect(screen.getByTestId("recomposite-bar")).toHaveAttribute("data-run", "r1");
    expect(screen.getByTestId("recomposite-feather")).toHaveAttribute("type", "number");
    expect(screen.getByTestId("recomposite-go")).toHaveTextContent("Re-mask");
    expect(screen.getByTestId("recomposite-free")).toHaveTextContent("0.00 USD");
  });

  it("mounted bare, follows the panels' selected run and its candidate's raw layer", () => {
    render(<RecompositeBar />);
    expect(screen.queryByTestId("recomposite-bar")).toBeNull();
    act(() => {
      useRunStore.getState().upsertRun(made("r1"), "edit-1");
      usePanelsStore.setState({ selectedRunId: "r1" });
    });
    expect(screen.queryByTestId("recomposite-bar")).toBeNull(); // selected, but no raw layer in its run
    act(() => {
      useRunStore.getState().upsertRun(made("r1", { normalized: { raw_key: RAW } }), "edit-1");
    });
    expect(screen.getByTestId("recomposite-bar")).toHaveAttribute("data-run", "r1");
    act(() => {
      usePanelsStore.setState({ selectedRunId: null });
    });
    expect(screen.queryByTestId("recomposite-bar")).toBeNull();
  });

  it("posts the route with grow and feather, shows the new run id and adds it to the strip", async () => {
    useRunStore.getState().upsertRun(made("r1", { normalized: { raw_key: RAW } }), "edit-1");
    const calls = mockApi({ "POST /runs/r1/recomposite": made("r2", { attempt: 2 }) });
    render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);

    type("recomposite-grow", "8");
    type("recomposite-feather", "6");
    fireEvent.click(screen.getByTestId("recomposite-go"));

    await waitFor(() => expect(screen.getByTestId("recomposite-run")).toHaveTextContent("r2"));
    expect(calls).toEqual([{ method: "POST", path: "/runs/r1/recomposite", body: { feather_px: 6, grow_px: 8 } }]);
    expect(screen.getByTestId("recomposite-made")).toHaveTextContent("DONE · 0.00 USD");
    expect(screen.queryByTestId("recomposite-error")).toBeNull();
    const store = useRunStore.getState();
    expect(store.candidates.r2?.state).toBe("DONE");
    expect(store.candidates.r2?.nodeId).toBe("edit-1");
    expect(store.byNode["edit-1"]?.runIds).toEqual(["r1", "r2"]);
  });

  it("sends neither field when they are left alone, and a shrink as a negative number", async () => {
    const calls = mockApi({ "POST /runs/r1/recomposite": made("r2") });
    render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);
    fireEvent.click(screen.getByTestId("recomposite-go"));
    await waitFor(() => expect(screen.getByTestId("recomposite-run")).toHaveTextContent("r2"));
    expect(calls[0]?.body).toEqual({});

    type("recomposite-grow", "-4");
    fireEvent.click(screen.getByTestId("recomposite-go"));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]?.body).toEqual({ grow_px: -4 });
  });

  it("ignores a number outside the limits as it is typed", () => {
    render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);
    type("recomposite-grow", "40");
    expect(screen.getByTestId("recomposite-go")).toBeEnabled();
    expect(screen.queryByTestId("recomposite-problem")).toBeNull();
  });

  it("says why the daemon refused", async () => {
    mockApi({
      "POST /runs/r1/recomposite": json(
        { error: "not_recompositable", detail: "run r1 cannot be re-composited: its raw take is not stored" },
        422,
      ),
    });
    render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);
    fireEvent.click(screen.getByTestId("recomposite-go"));
    await waitFor(() => expect(screen.getByTestId("recomposite-error")).toHaveTextContent("raw take is not stored"));
    expect(screen.queryByTestId("recomposite-made")).toBeNull();
    expect(screen.getByTestId("recomposite-go")).toBeEnabled(); // the artist can fix the Mask and try again
  });

  it("starts over for another take", () => {
    mockApi({ "POST /runs/r1/recomposite": made("r2") });
    const { rerender } = render(<RecompositeBar runId="r1" nodeId="edit-1" rawKey={RAW} />);
    type("recomposite-grow", "5");
    rerender(<RecompositeBar runId="r3" nodeId="edit-1" rawKey={RAW} />);
    expect(screen.getByTestId("recomposite-bar")).toHaveAttribute("data-run", "r3");
    expect(screen.getByTestId("recomposite-grow")).toHaveValue(0);
  });
});

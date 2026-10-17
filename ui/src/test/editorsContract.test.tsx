// T70 editor contracts with the daemon routes of wave 8: the Mask editor uploads a painted mask into the open
// project (T56), its pick chips show what `/pixels/idpick` now says (T62: locked, coverage, depth_m), text lock is a
// view (decision 34), the Light editor follows its plugin job until the dock answers (T48 queue, `GET /plugin/jobs/{id}`)
// and the Motion editor reads the router preview in the flat shape T73 serves. The daemon is a fetch stub that
// answers by "METHOD /path" and records every call, as in editors.test.tsx.

import { act, cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPluginJob, parseIdPick, parseRouterPreview, queuePluginJob, routerPreview, uploadMask } from "../editors/api";
import { JOB_POLL_MS, jobSteps, LightEditor } from "../editors/LightEditor";
import { clearPaintLayers, MaskEditor } from "../editors/MaskEditor";
import { MotionEditor } from "../editors/MotionEditor";
import { FALLBACK_ENGINES } from "../makebar/engines";
import { EMPTY_ENTRY, useMakeStore } from "../makebar/makeStore";
import type { NodeKind } from "../nodeTypes";
import { createInitialGraph, useGraphStore } from "../store";

// the suite shares a busy machine with the daemon and plugin test runs: give async queries room, not retries
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      const body = typeof raw === "string" ? JSON.parse(raw) : (raw ?? null);
      calls.push({ method, path, search: url.search, body });
      const route = routes[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(calls[calls.length - 1] as Call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const posts = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "POST" && c.path === path);
const gets = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "GET" && c.path === path);

// ------------------------------------------------------------------------------------------------ fixtures

const PLATE = { width: 5504, height: 3072 };

/** Plate -> `kind` node, `projectId` open (null = no project), the node anchored to a packet. */
function openGraph(kind: NodeKind, extra: Record<string, unknown> = {}, projectId: string | null = "p1"): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE },
        { id: "n1", kind, label: kind, ...extra },
      ],
      edges: [{ id: "plate-1->n1", source: "plate-1", target: "n1", sourceHandle: null, targetHandle: null }],
    } as never,
    projectId,
  );
}

const paramsOf = (): Record<string, unknown> => useGraphStore.getState().nodes.find((n) => n.id === "n1")?.data.params ?? {};

const drawn = { putImageData: vi.fn(), createImageData: vi.fn() };

const mount = (ui: ReactElement) => render(ui);

const pointerAt = (stage: HTMLElement, fx: number, fy: number, type: "pointerDown" | "pointerMove" | "pointerUp" = "pointerDown"): void => {
  fireEvent[type](stage, { clientX: fx * 1280, clientY: fy * 720, pointerId: 1 });
};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  clearPaintLayers();
  window.sessionStorage.clear();
  drawn.putImageData.mockReset();
  drawn.createImageData.mockReset().mockImplementation((w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h }));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => drawn) as never);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ================================================================================================ Mask: upload

/** Paint a stroke so Save mask has something to send. */
function paintSomething(): void {
  fireEvent.click(screen.getByTestId("mask-tab-paint"));
  const stage = screen.getByTestId("mask-stage");
  pointerAt(stage, 0.25, 0.5);
  pointerAt(stage, 0.75, 0.5, "pointerMove");
  pointerAt(stage, 0.75, 0.5, "pointerUp");
}

describe("Mask upload carries the project (T56)", () => {
  it("uploadMask posts to /assets?kind=mask&project_id=<id> with the PNG bytes and returns the hash", async () => {
    const calls = mockApi({ "POST /assets": { hash: "abc123", width: 4, height: 4, project_id: "p 1/x" } });
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
    expect(await uploadMask(png, "p 1/x")).toBe("abc123");
    const up = posts(calls, "/assets")[0];
    expect(up?.search).toBe("?kind=mask&project_id=p%201%2Fx");
    expect(up?.body).toBeInstanceOf(Uint8Array);
  });

  it("uploadMask rejects an answer with no hash", async () => {
    mockApi({ "POST /assets": { width: 4 } });
    await expect(uploadMask(new Uint8Array([1]), "p1")).rejects.toThrow(/no hash/);
  });

  it("Save mask sends the open project's id and stores the returned hash on the node", async () => {
    const calls = mockApi({ "POST /assets": { hash: "mask_hash_0123456789" } });
    openGraph("Mask", {}, "proj_42");
    mount(<MaskEditor nodeId="n1" />);
    paintSomething();
    fireEvent.click(screen.getByTestId("mask-paint-save"));
    await waitFor(() => expect(paramsOf().maskHash).toBe("mask_hash_0123456789"));
    const up = calls.find((c) => c.method === "POST" && c.path === "/assets");
    expect(up?.search).toBe("?kind=mask&project_id=proj_42&packet_id=room_a"); // T82: the plate's packet rides along
    expect(Array.from((up?.body as Uint8Array).subarray(0, 4))).toEqual([0x89, 0x50, 0x4e, 0x47]);
  });

  it("without an open project the mask cannot be saved (the button is off) rather than sent without a project", () => {
    const calls = mockApi({ "POST /assets": { hash: "never" } });
    openGraph("Mask", {}, null);
    mount(<MaskEditor nodeId="n1" />);
    paintSomething();
    expect(screen.getByTestId("mask-paint-save")).toBeDisabled();
    fireEvent.click(screen.getByTestId("mask-paint-save"));
    expect(posts(calls, "/assets")).toHaveLength(0);
  });
});

// ================================================================================================ Mask: text lock is a view

describe("Mask text lock is a view (decision 34)", () => {
  it("the toggle says it shows the OCR boxes and carries the policy tooltip", async () => {
    mockApi({ "POST /pixels/ocr": { shape: [3072, 5504], boxes: [{ bbox: [100, 200, 900, 400], text: "TULA", conf: 0.9 }] } });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-text"));
    const toggle = screen.getByTestId("mask-text-lock");
    const label = toggle.closest("label");
    expect(label).toHaveTextContent("show OCR text-lock boxes");
    expect(label).toHaveAttribute("title", "signage is always locked by the packet policy");
    expect(screen.queryByText(/lock text/)).toBeNull();
    fireEvent.click(toggle);
    expect(paramsOf().textLock).toBe(true);
    await waitFor(() => expect(screen.getAllByTestId("mask-ocr-box")).toHaveLength(1));
  });
});

// ================================================================================================ Mask: pick chips

describe("idpick chips: locked, coverage, depth_m (T62)", () => {
  it("parseIdPick reads locked, coverage and depth_m; a locked id is never editable", () => {
    const r = parseIdPick({ id: "o:dc37ac5e", name: "VEG_shrub_A", class: "vegetation", editable: true, locked: false, coverage: 0.87, depth_m: 6.254 });
    expect(r).toMatchObject({ locked: false, coverage: 0.87, depthM: 6.254, editable: true });
    const locked = parseIdPick({ id: "o:1", name: "ARCH_wall", class: "vegetation", locked: true });
    expect(locked.locked).toBe(true);
    expect(locked.editable).toBe(false);
    expect(parseIdPick({ id: "o:2", name: "x", class: "ground" })).toMatchObject({ locked: null, coverage: null, depthM: null });
  });

  it("an id the policy locks shows a locked chip, coverage and depth, and does not join the mask", async () => {
    mockApi({ "POST /pixels/idpick": { id: "o:aa000001", name: "ARCH_facade", class: "architecture", kind: "object", editable: false, locked: true, coverage: 1, depth_m: 12.5 } });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    const locked = await screen.findByTestId("mask-pick-locked");
    expect(locked).toHaveTextContent(/^locked$/);
    expect(locked).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("mask-pick-coverage")).toHaveTextContent("coverage 100%");
    expect(screen.getByTestId("mask-pick-depth")).toHaveTextContent("depth 12.50 m");
    expect(screen.getByTestId("mask-pick-name")).toHaveTextContent("ARCH_facade");
    expect(screen.getByTestId("mask-note")).toHaveTextContent(/ARCH_facade is locked by the packet policy/);
    expect(screen.queryByTestId("mask-chip")).toBeNull();
    expect(paramsOf().ids).toBeUndefined();
  });

  it("an open id shows not locked and still joins the mask; fields the daemon omits show no chip", async () => {
    mockApi({
      "POST /pixels/idpick": (c: Call) =>
        (c.body as { x: number }).x < 2000
          ? { id: "o:dc37ac5e", name: "VEG_shrub_A", class: "vegetation", editable: true, locked: false, coverage: 0.5 }
          : { id: "o:dc37ac5f", name: "VEG_palm_B", class: "vegetation" },
    });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    const stage = screen.getByTestId("mask-stage");
    pointerAt(stage, 0.1, 0.5);
    const locked = await screen.findByTestId("mask-pick-locked");
    expect(locked).toHaveTextContent("not locked");
    expect(locked).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("mask-pick-coverage")).toHaveTextContent("coverage 50%");
    expect(screen.queryByTestId("mask-pick-depth")).toBeNull();
    expect(await screen.findByTestId("mask-chip")).toHaveAttribute("data-id", "o:dc37ac5e");

    pointerAt(stage, 0.9, 0.5);
    await waitFor(() => expect(screen.getByTestId("mask-pick-name")).toHaveTextContent("VEG_palm_B"));
    expect(screen.queryByTestId("mask-pick-locked")).toBeNull();
    expect(screen.queryByTestId("mask-pick-coverage")).toBeNull();
  });

  it("a pixel with no id clears the last-pick chips", async () => {
    let next: unknown = { id: "o:dc37ac5e", name: "VEG_shrub_A", class: "vegetation", locked: false, depth_m: 3 };
    mockApi({ "POST /pixels/idpick": () => next });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    const stage = screen.getByTestId("mask-stage");
    pointerAt(stage, 0.5, 0.5);
    await screen.findByTestId("mask-pick-depth");
    next = { id: null };
    pointerAt(stage, 0.2, 0.2);
    await waitFor(() => expect(screen.queryByTestId("mask-pick")).toBeNull());
  });
});

// ================================================================================================ Light: the plugin job

describe("getPluginJob (GET /plugin/jobs/{id})", () => {
  it("reads state, the dock's detail and the packet it sent", async () => {
    mockApi({
      "GET /plugin/jobs/job_1": { id: "job_1", state: "done", result: { ok: true, detail: "re-rendered", packet_id: "room_a_v2", metrics: { de00: 1.2 } } },
      "GET /plugin/jobs/job_2": { id: "job_2", state: "claimed", result: null },
      "GET /plugin/jobs/job_3": { id: "job_3", state: "cancelled", result: { cancelled: true, reason: "run cancelled" } },
    });
    expect(await getPluginJob("job_1")).toEqual({ id: "job_1", state: "done", detail: "re-rendered", resultPacketId: "room_a_v2" });
    expect(await getPluginJob("job_2")).toEqual({ id: "job_2", state: "claimed", detail: null, resultPacketId: null });
    expect(await getPluginJob("job_3")).toMatchObject({ state: "cancelled", detail: "run cancelled" });
  });

  it("queuePluginJob still takes id or job_id and rejects an answer with neither", async () => {
    mockApi({ "POST /plugin/jobs": { job_id: "job_9" } });
    expect(await queuePluginJob({ action: "light.vantage_preview", packet_id: "room_a" })).toMatchObject({ id: "job_9", state: "queued" });
    mockApi({ "POST /plugin/jobs": { state: "queued" } });
    await expect(queuePluginJob({ action: "light.match" })).rejects.toThrow(/no id/);
  });

  it("jobSteps ends where the job ended", () => {
    expect(jobSteps("queued")).toEqual(["queued", "claimed", "done"]);
    expect(jobSteps("done")).toEqual(["queued", "claimed", "done"]);
    expect(jobSteps("failed")).toEqual(["queued", "claimed", "failed"]);
    expect(jobSteps("cancelled")).toEqual(["queued", "claimed", "cancelled"]);
  });
});

describe("LightEditor follows its plugin job", () => {
  /** Only the interval is faked: fetch, promises and Testing Library's own timers stay real. */
  const fakeIntervals = (): void => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  };
  const tick = async (ms: number = JOB_POLL_MS): Promise<void> => {
    await act(async () => {
      vi.advanceTimersByTime(ms);
    });
  };
  /**
   * Wait, in real macrotask turns, until `ready()` holds. The editor arms and clears its interval in passive effects,
   * which React runs a scheduler turn after the commit a query has already seen. Advancing the fake clock before the
   * effect has run (or still counting a cleared interval) is the race this closes; the machine load decides how late
   * those effects are, so no fixed delay would do.
   */
  async function until(ready: () => boolean, what: string): Promise<void> {
    const deadline = Date.now() + 15_000;
    while (!ready() && Date.now() < deadline) {
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 5));
      });
    }
    if (!ready()) throw new Error(`never settled: ${what}`);
  }
  /** The editor's poll interval is the only fake timer there is: exactly `n` of them are armed. */
  const armed = (n: number): Promise<void> => until(() => vi.getTimerCount() === n, `${n} poll interval(s) armed`);

  async function startMatch(): Promise<void> {
    openGraph("Light", { params: { referenceHash: "ref_hash_1" } });
    mount(<LightEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("light-match"));
    await screen.findByTestId("light-job");
    await armed(1);
  }

  it("polls every 3 s: queued, then claimed, then done with the dock's detail and packet; then stops", async () => {
    fakeIntervals();
    const answers: unknown[] = [
      { id: "job_7", state: "claimed", result: null },
      { id: "job_7", state: "done", result: { ok: true, detail: "matched Cam_Lobby, de00 1.1", packet_id: "room_a_v3" } },
    ];
    const calls = mockApi({
      "POST /plugin/jobs": { id: "job_7", state: "queued" },
      "GET /plugin/jobs/job_7": () => answers.shift() ?? { id: "job_7", state: "done", result: { ok: true } },
    });
    await startMatch();
    const chip = screen.getByTestId("light-job");
    expect(chip).toHaveTextContent("match queued: job job_7 (queued)");
    expect(screen.getByTestId("light-job-steps")).toHaveTextContent("queued → claimed → done");
    expect(gets(calls, "/plugin/jobs/job_7")).toHaveLength(0);

    // nothing is asked before the first 3 s are up
    await tick(JOB_POLL_MS - 1);
    expect(gets(calls, "/plugin/jobs/job_7")).toHaveLength(0);

    await tick(1);
    expect(gets(calls, "/plugin/jobs/job_7")).toHaveLength(1);
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("match claimed by Max: job job_7 (claimed)"));
    expect(screen.getByTestId("light-job")).toHaveAttribute("data-tone", "info");
    expect(screen.getByTestId("light-job-steps").querySelector("[data-active='true']")).toHaveTextContent("claimed");
    expect(screen.queryByTestId("light-job-packet")).toBeNull();

    await tick();
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("match done: job job_7 (done)"));
    expect(screen.getByTestId("light-job")).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("light-job-detail")).toHaveTextContent("matched Cam_Lobby, de00 1.1");
    expect(screen.getByTestId("light-job-packet")).toHaveTextContent("packet room_a_v3");
    expect(gets(calls, "/plugin/jobs/job_7")).toHaveLength(2);

    // a finished job is not asked about again
    await armed(0);
    await tick();
    await tick();
    expect(gets(calls, "/plugin/jobs/job_7")).toHaveLength(2);
  });

  it("a failed job shows the dock's reason and stops polling", async () => {
    fakeIntervals();
    const calls = mockApi({
      "POST /plugin/jobs": { id: "job_8", state: "queued" },
      "GET /plugin/jobs/job_8": { id: "job_8", state: "failed", result: { ok: false, detail: "MaxGaffer is not installed" } },
    });
    await startMatch();
    await tick();
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("match failed: job job_8 (failed)"));
    expect(screen.getByTestId("light-job")).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("light-job-detail")).toHaveTextContent("MaxGaffer is not installed");
    expect(screen.getByTestId("light-job-steps")).toHaveTextContent("queued → claimed → failed");
    expect(screen.queryByTestId("light-job-packet")).toBeNull();
    await armed(0);
    await tick();
    expect(gets(calls, "/plugin/jobs/job_8")).toHaveLength(1);
  });

  it("stops polling when the editor is closed", async () => {
    fakeIntervals();
    const calls = mockApi({
      "POST /plugin/jobs": { id: "job_9", state: "queued" },
      "GET /plugin/jobs/job_9": { id: "job_9", state: "claimed", result: null },
    });
    openGraph("Light");
    const view = mount(<LightEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("light-vantage"));
    await screen.findByTestId("light-job");
    await armed(1);
    await tick();
    await waitFor(() => expect(gets(calls, "/plugin/jobs/job_9")).toHaveLength(1));
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await tick();
    await tick();
    expect(gets(calls, "/plugin/jobs/job_9")).toHaveLength(1);
  });

  it("a job the daemon no longer knows stops the polling and says so; a hiccup is retried", async () => {
    fakeIntervals();
    let n = 0;
    const calls = mockApi({
      "POST /plugin/jobs": { id: "job_10", state: "queued" },
      "GET /plugin/jobs/job_10": () => {
        n += 1;
        if (n === 1) return jsonResponse({ detail: "daemon busy" }, 503);
        if (n === 2) return { id: "job_10", state: "claimed", result: null };
        return jsonResponse({ error: "not_found" }, 404);
      },
    });
    await startMatch();
    await tick();
    expect(await screen.findByTestId("light-job-error")).toHaveTextContent(/could not read the job's state \(will retry\)/);
    expect(screen.getByTestId("light-job")).toHaveTextContent("(queued)");
    await tick();
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("(claimed)"));
    expect(screen.queryByTestId("light-job-error")).toBeNull();
    await tick();
    expect(await screen.findByTestId("light-job-error")).toHaveTextContent(/no longer has this job/);
    await armed(0);
    await tick();
    expect(gets(calls, "/plugin/jobs/job_10")).toHaveLength(3);
  });

  it("a second job replaces the first one's watch", async () => {
    fakeIntervals();
    const calls = mockApi({
      "POST /plugin/jobs": (c: Call) => ((c.body as { action: string }).action === "light.match" ? { id: "job_a", state: "queued" } : { id: "job_b", state: "queued" }),
      "GET /plugin/jobs/job_a": { id: "job_a", state: "claimed", result: null },
      "GET /plugin/jobs/job_b": { id: "job_b", state: "done", result: { ok: true, packet_id: "pv_1" } },
    });
    await startMatch();
    const armedIntervals = vi.spyOn(globalThis, "setInterval");
    fireEvent.click(screen.getByTestId("light-vantage"));
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("vantage preview queued: job job_b (queued)"));
    // job_a's interval is cleared and job_b's armed in one effect pass: wait for that pass, not for the DOM
    await until(() => armedIntervals.mock.calls.length >= 1 && vi.getTimerCount() === 1, "job_b's interval armed");
    await tick();
    await waitFor(() => expect(screen.getByTestId("light-job")).toHaveTextContent("vantage preview done: job job_b (done)"));
    expect(gets(calls, "/plugin/jobs/job_a")).toHaveLength(0);
    expect(screen.getByTestId("light-job-packet")).toHaveTextContent("packet pv_1");
  });
});

// ================================================================================================ Motion: router preview

describe("Motion router preview (T73 shape)", () => {
  const FLAT = {
    verdict: "rendered_endpoints_plus_clay",
    mode: "advisory",
    D_px_median: 22.4,
    disocclusion_fraction: 0.035,
    orbit_deg: 12,
    explanation: "3.5 % of end-frame pixels unseen by the start frame, orbit 12.0 deg",
  };
  const NESTED = {
    mode: "blocking",
    verdict: "render_move",
    numbers: { D_px_median: 6, disocclusion_fraction: 0.2, orbit_deg: 41, detail_verdict: "v_ray_only", crossings: [{ at: 0.4 }] },
    explanation: "orbit 41 deg",
  };

  it("parseRouterPreview folds the flat numbers into a RouterCard and still reads the nested and wrapped forms", () => {
    expect(parseRouterPreview(FLAT)).toEqual({
      mode: "advisory",
      verdict: "rendered_endpoints_plus_clay",
      numbers: { D_px_median: 22.4, disocclusion_fraction: 0.035, orbit_deg: 12, detail_verdict: null, crossings: 0 },
      explanation: FLAT.explanation,
    });
    expect(parseRouterPreview(NESTED)).toMatchObject({ mode: "blocking", verdict: "render_move", numbers: { orbit_deg: 41, detail_verdict: "v_ray_only", crossings: 1 } });
    expect(parseRouterPreview({ router: FLAT })?.numbers.D_px_median).toBe(22.4);
    expect(parseRouterPreview({ router: NESTED })?.mode).toBe("blocking");
    expect(parseRouterPreview({ verdict: "nonsense" })).toBeNull();
    expect(parseRouterPreview(null)).toBeNull();
    expect(parseRouterPreview("router")).toBeNull();
  });

  it('routerPreview posts what: "router" with the params to the node preview route', async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": FLAT });
    const card = await routerPreview("p1", "n1", { move: "push_in", duration: 6 });
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "router", params: { move: "push_in", duration: 6 } });
    expect(card?.verdict).toBe("rendered_endpoints_plus_clay");
    expect(card?.numbers.disocclusion_fraction).toBe(0.035);
  });

  it("the Motion editor shows the card from the flat answer", async () => {
    useMakeStore.setState({ engines: { engines: [...FALLBACK_ENGINES], source: "fallback" }, custody: null, custodyFor: null, entries: { n1: { ...EMPTY_ENTRY } } });
    mockApi({ "POST /projects/p1/nodes/n1/preview": FLAT });
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k", duration: 6 } });
    mount(<MotionEditor nodeId="n1" />);
    const card = await screen.findByTestId("motion-router");
    expect(card).toHaveAttribute("data-verdict", "rendered_endpoints_plus_clay");
    expect(card).toHaveAttribute("data-mode", "advisory");
    expect(screen.getByTestId("motion-router-disocc")).toHaveTextContent("3.5%");
    expect(screen.getByTestId("motion-router-orbit")).toHaveTextContent("12.0 deg");
    expect(screen.getByTestId("motion-router-d")).toHaveTextContent("22.4 px");
  });
});

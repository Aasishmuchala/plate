// T72: import a client plate or packet kit from the Project panel, and edit the project's custody. The daemon is a
// fetch stub that answers by "METHOD /path" and records every call, so each test also pins the request the form sent.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createInitialGraph, useGraphStore } from "../store";
import { importKit, importTierC, loosensCustody, putCustody } from "../panels/importApi";
import { ProjectPanel } from "../panels/ProjectPanel";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import type { ProjectCustody } from "../panels/types";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded. */
function mockApi(routes: Record<string, Route>): Call[] {
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
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const sent = (calls: Call[], method: string, path: string): Call[] => calls.filter((c) => c.method === method && c.path === path);

// ------------------------------------------------------------------------------------------------ fixtures

const OPEN_GRADES = ["C0", "C1", "C2", "D"] as const;

const project = (nda: boolean, grades: readonly string[]) => ({
  project_id: "p1",
  created_utc: "2026-10-18T09:00:00Z",
  custody: { nda, allowed_grades: [...grades] },
  engine_pins: {},
});

const GRAPH = { schema: 1 as const, nodes: [], edges: [] };

/** Opens project p1 (whose custody the stub lists) in the panel; `routes` adds or replaces daemon answers. */
async function showPanel(custody = project(false, OPEN_GRADES), routes: Record<string, Route> = {}): Promise<Call[]> {
  useGraphStore.getState().loadProject(GRAPH, "p1");
  const calls = mockApi({ "GET /projects": [custody], ...routes });
  render(<ProjectPanel />);
  await screen.findByTestId("custody-row");
  return calls;
}

const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  usePanelsStore.setState(initialPanelsState());
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------------------------------------ importApi

describe("importApi", () => {
  it("posts the Tier C and kit bodies to the project's routes and puts the custody", async () => {
    const calls = mockApi({
      "POST /projects/a%2Fb/tierc": { packet_id: "pk1", status: "ingesting" },
      "POST /projects/p1/kit": { packet_id: "pk2", status: "ingesting" },
      "PUT /projects/p1/custody": { project_id: "p1" },
    });
    await expect(importTierC("a/b", { plate_path: "/x/plate.png", shot_id: "s1", consent_record: "ok" })).resolves.toMatchObject({
      packet_id: "pk1",
    });
    await importKit("p1", { folder: "/kit", camera: "Cam01", plate_path: "/x/plate.png", shot_id: "s2" });
    await putCustody("p1", { nda: true, allowed_grades: ["C0", "C1"] });
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "POST /projects/a%2Fb/tierc",
      "POST /projects/p1/kit",
      "PUT /projects/p1/custody",
    ]);
    expect(calls[0].body).toEqual({ plate_path: "/x/plate.png", shot_id: "s1", consent_record: "ok" });
    expect(calls[2].body).toEqual({ nda: true, allowed_grades: ["C0", "C1"] });
  });

  it("calls turning NDA on tightening and anything that adds a grade or drops NDA loosening", () => {
    const open: ProjectCustody = { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] };
    const nda: ProjectCustody = { nda: true, allowed_grades: ["C0", "C1"] };
    expect(loosensCustody(open, { nda: true, allowed_grades: ["C0", "C1"] })).toBe(false);
    expect(loosensCustody(open, { nda: false, allowed_grades: ["C0", "C1", "C2"] })).toBe(false);
    expect(loosensCustody(open, open)).toBe(false);
    expect(loosensCustody(nda, { nda: false, allowed_grades: ["C0", "C1"] })).toBe(true);
    expect(loosensCustody({ nda: false, allowed_grades: ["C0", "C1"] }, { nda: false, allowed_grades: ["C0", "C1", "C2"] })).toBe(true);
    expect(loosensCustody(nda, { nda: true, allowed_grades: ["C1"] })).toBe(false);
  });
});

// ------------------------------------------------------------------------------------------------ import form

describe("import a client plate", () => {
  const open = () => fireEvent.click(screen.getByRole("button", { name: "Import client plate" }));

  it("keeps the form closed until the disclosure is opened", async () => {
    await showPanel();
    expect(screen.queryByLabelText("Plate path")).toBeNull();
    open();
    expect(screen.getByRole("button", { name: "Import client plate" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Plate path")).toBeInTheDocument();
    expect(screen.getByLabelText("Tier C plate")).toBeChecked();
    expect(screen.getByLabelText("Client kit")).not.toBeChecked();
  });

  it("refuses a Tier C plate without a consent record, locally and with the daemon's words", async () => {
    const calls = await showPanel();
    open();
    type("Plate path", "/clients/tula/plate.png");
    type("Shot id", "s01");
    fireEvent.click(screen.getByRole("button", { name: "Import Tier C plate" }));
    expect(await screen.findByText("consent record required")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Consent record"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Import Tier C plate" }));
    expect(await screen.findByText("consent record required")).toBeInTheDocument();
    expect(sent(calls, "POST", "/projects/p1/tierc")).toHaveLength(0);
  });

  it("posts the plate, shot and consent, then shows the packet as ingesting", async () => {
    const calls = await showPanel(project(false, OPEN_GRADES), {
      "POST /projects/p1/tierc": jsonResponse(
        { packet_id: "pk_01", status: "ingesting", tier: "C", packet_dir: "/p/tierc", events_url: "/api/v0/packets/pk_01/events", stub: false },
        202,
      ),
    });
    open();
    type("Plate path", " /clients/tula/plate.png ");
    type("Shot id", "s01");
    type("Consent record", "  Written consent from the client, 2026-10-18.  ");
    fireEvent.click(screen.getByRole("button", { name: "Import Tier C plate" }));
    expect(await screen.findByTestId("import-result")).toHaveTextContent("packet pk_01 ingesting");
    expect(sent(calls, "POST", "/projects/p1/tierc")).toHaveLength(1);
    expect(sent(calls, "POST", "/projects/p1/tierc")[0].body).toEqual({
      plate_path: "/clients/tula/plate.png",
      shot_id: "s01",
      consent_record: "Written consent from the client, 2026-10-18.",
    });
    // the consent belonged to that plate: the form is empty for the next one
    expect(screen.getByLabelText("Plate path")).toHaveValue("");
    expect(screen.getByLabelText("Consent record")).toHaveValue("");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the daemon's refusal as it sent it", async () => {
    await showPanel(project(false, OPEN_GRADES), {
      "POST /projects/p1/tierc": jsonResponse({ error: "consent record required" }, 428),
    });
    open();
    type("Plate path", "/clients/tula/plate.png");
    type("Shot id", "s01");
    type("Consent record", "a record");
    fireEvent.click(screen.getByRole("button", { name: "Import Tier C plate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("consent record required");
    expect(screen.queryByTestId("import-result")).toBeNull();
    // a refused import keeps what was typed
    expect(screen.getByLabelText("Plate path")).toHaveValue("/clients/tula/plate.png");
  });

  it("shows a pixels-worker outage inline", async () => {
    await showPanel(project(false, OPEN_GRADES), {
      "POST /projects/p1/tierc": jsonResponse({ error: "pixels_unavailable", detail: "the pixels worker is not reachable on 127.0.0.1:4611" }, 503),
    });
    open();
    type("Plate path", "/clients/tula/plate.png");
    type("Shot id", "s01");
    type("Consent record", "a record");
    fireEvent.click(screen.getByRole("button", { name: "Import Tier C plate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("the pixels worker is not reachable");
  });

  it("posts a kit with its folder, camera and plate, and shows what the converter warned about", async () => {
    const calls = await showPanel(project(false, OPEN_GRADES), {
      "POST /projects/p1/kit": jsonResponse(
        { packet_id: "pk_02", status: "ingesting", tier: "A", flags: ["client_plate_8bit"], warnings: ["depth_kind assumed planar"] },
        202,
      ),
    });
    open();
    fireEvent.click(screen.getByLabelText("Client kit"));
    expect(screen.queryByLabelText("Consent record")).toBeNull();
    type("Kit folder", "/clients/tula/kit");
    type("Camera name", "Cam01");
    type("Plate path", "/clients/tula/cam01.png");
    fireEvent.click(screen.getByRole("button", { name: "Import client kit" }));
    const result = await screen.findByTestId("import-result");
    expect(result).toHaveTextContent("packet pk_02 ingesting");
    expect(within(result).getByText("client_plate_8bit")).toBeInTheDocument();
    expect(within(result).getByText("depth_kind assumed planar")).toBeInTheDocument();
    expect(sent(calls, "POST", "/projects/p1/kit")[0].body).toEqual({
      folder: "/clients/tula/kit",
      camera: "Cam01",
      plate_path: "/clients/tula/cam01.png",
    });
    // the kit folder stays for the next camera
    expect(screen.getByLabelText("Kit folder")).toHaveValue("/clients/tula/kit");
    expect(screen.getByLabelText("Camera name")).toHaveValue("");
  });

  it("sends the shot id of a kit when one is given and asks for the missing fields otherwise", async () => {
    const calls = await showPanel(project(false, OPEN_GRADES), {
      "POST /projects/p1/kit": jsonResponse({ packet_id: "pk_03", status: "ingested" }, 202),
    });
    open();
    fireEvent.click(screen.getByLabelText("Client kit"));
    fireEvent.click(screen.getByRole("button", { name: "Import client kit" }));
    expect(await screen.findByText("Kit folder is required.")).toBeInTheDocument();
    expect(screen.getByText("Camera name is required.")).toBeInTheDocument();
    expect(screen.getByText("Plate path is required.")).toBeInTheDocument();
    type("Kit folder", "/kit");
    type("Camera name", "Cam02");
    type("Plate path", "/plate.png");
    type("Shot id", "s02");
    fireEvent.click(screen.getByRole("button", { name: "Import client kit" }));
    expect(await screen.findByTestId("import-result")).toHaveTextContent("packet pk_03 ingested");
    expect(sent(calls, "POST", "/projects/p1/kit")).toHaveLength(1);
    expect(sent(calls, "POST", "/projects/p1/kit")[0].body).toEqual({
      folder: "/kit",
      camera: "Cam02",
      plate_path: "/plate.png",
      shot_id: "s02",
    });
  });

  it("cannot submit while no project is open", async () => {
    mockApi({ "GET /projects": [] });
    render(<ProjectPanel />);
    await screen.findByText("No projects yet. Create one below.");
    open();
    expect(screen.getByText("Open a project to import into it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import Tier C plate" })).toBeDisabled();
  });
});

// ------------------------------------------------------------------------------------------------ custody row

describe("custody row", () => {
  const edit = () => fireEvent.click(screen.getByRole("button", { name: "Edit custody" }));

  it("shows the open project's custody with an edit button, closed by default", async () => {
    await showPanel(project(true, ["C0", "C1"]));
    const row = screen.getByTestId("custody-current");
    expect(within(row).getByText("NDA")).toBeInTheDocument();
    expect(within(row).getByText("C1")).toBeInTheDocument();
    expect(screen.queryByTestId("custody-edit")).toBeNull();
  });

  it("asks for a reason before loosening an NDA project, and does not post without one", async () => {
    const calls = await showPanel(project(true, ["C0", "C1"]));
    edit();
    fireEvent.click(screen.getByLabelText(/^NDA on/));
    expect(screen.getByLabelText("Custody allow C2")).not.toBeDisabled();
    fireEvent.click(screen.getByLabelText("Custody allow C2"));
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    expect(await screen.findByText("reason required")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Custody reason"), { target: { value: "too short" } });
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    expect(await screen.findByText("reason must be at least 10 characters")).toBeInTheDocument();
    expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(0);
  });

  it("posts a loosening with its reason, then closes the editor and reloads the list", async () => {
    let listed = project(true, ["C0", "C1"]);
    const calls = mockApi({
      "GET /projects": () => [listed],
      "PUT /projects/p1/custody": (c: Call) => {
        const b = c.body as { nda: boolean; allowed_grades: string[] };
        listed = project(b.nda, b.allowed_grades);
        return { project_id: "p1", custody: listed.custody };
      },
    });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    fireEvent.click(screen.getByLabelText(/^NDA on/));
    fireEvent.click(screen.getByLabelText("Custody allow C2"));
    expect(screen.getByTestId("custody-edit")).toHaveTextContent("This loosens the project's custody");
    fireEvent.change(screen.getByLabelText("Custody reason"), { target: { value: "  client signed off the cloud route  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    await waitFor(() => expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(1));
    expect(sent(calls, "PUT", "/projects/p1/custody")[0].body).toEqual({
      nda: false,
      allowed_grades: ["C0", "C1", "C2"],
      reason: "client signed off the cloud route",
    });
    await waitFor(() => expect(screen.queryByTestId("custody-edit")).toBeNull());
    await waitFor(() => expect(within(screen.getByTestId("custody-current")).getByText("C2")).toBeInTheDocument());
    expect(within(screen.getByTestId("custody-current")).queryByText("NDA")).toBeNull();
    expect(sent(calls, "GET", "/projects").length).toBeGreaterThanOrEqual(2);
  });

  it("posts a tightening with no reason", async () => {
    const calls = mockApi({
      "GET /projects": [project(false, OPEN_GRADES)],
      "PUT /projects/p1/custody": { project_id: "p1" },
    });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    expect(screen.getByRole("button", { name: "Save custody" })).toBeDisabled(); // nothing changed yet
    fireEvent.click(screen.getByLabelText("Custody allow D"));
    expect(screen.getByTestId("custody-edit")).not.toHaveTextContent("This loosens");
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    await waitFor(() => expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(1));
    expect(sent(calls, "PUT", "/projects/p1/custody")[0].body).toEqual({ nda: false, allowed_grades: ["C0", "C1", "C2"] });
  });

  it("turning NDA on locks C2 and D and posts C0 and C1", async () => {
    const calls = mockApi({
      "GET /projects": [project(false, OPEN_GRADES)],
      "PUT /projects/p1/custody": { project_id: "p1" },
    });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    fireEvent.click(screen.getByLabelText(/^NDA on/));
    expect(screen.getByLabelText("Custody allow C2")).toBeDisabled();
    expect(screen.getByLabelText("Custody allow C2")).not.toBeChecked();
    expect(screen.getByLabelText("Custody allow D")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    await waitFor(() => expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(1));
    expect(sent(calls, "PUT", "/projects/p1/custody")[0].body).toEqual({ nda: true, allowed_grades: ["C0", "C1"] });
  });

  it("refuses an empty grade set locally", async () => {
    const calls = mockApi({ "GET /projects": [project(false, ["C0"])] });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    fireEvent.click(screen.getByLabelText("Custody allow C0"));
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Allow at least one custody grade.");
    expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(0);
  });

  it("shows the daemon's refusal inline and keeps the editor open", async () => {
    mockApi({
      "GET /projects": [project(true, ["C0", "C1"])],
      "PUT /projects/p1/custody": jsonResponse({ detail: "loosening needs a reason of at least 10 characters" }, 422),
    });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    fireEvent.click(screen.getByLabelText(/^NDA on/));
    fireEvent.change(screen.getByLabelText("Custody reason"), { target: { value: "a long enough reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Save custody" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("loosening needs a reason of at least 10 characters");
    expect(screen.getByTestId("custody-edit")).toBeInTheDocument();
  });

  it("cancel closes the editor without posting", async () => {
    const calls = mockApi({ "GET /projects": [project(false, OPEN_GRADES)] });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    edit();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("custody-edit")).toBeNull();
    expect(sent(calls, "PUT", "/projects/p1/custody")).toHaveLength(0);
  });
});
